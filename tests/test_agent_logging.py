"""Logging must never block or kill the agent (2026-10-10 08:15: a hung write on a stale log handle froze every
printing thread in all six processes)."""
import io, sys, tempfile, threading, time, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import agent_logging


class BlockingHandle(io.StringIO):
    def write(self, s):
        threading.Event().wait()          # never returns: the disk is gone


class FailingOnce:
    def __init__(self):
        self.failed = False
    def write(self, s):
        if not self.failed:
            self.failed = True
            raise OSError("stale handle")
    def flush(self):
        pass
    def close(self):
        pass


class TeeNeverBlocks(unittest.TestCase):
    def test_print_returns_while_the_file_write_hangs(self):
        tee = agent_logging._Tee(None, BlockingHandle(), None)
        t0 = time.time()
        for _ in range(50):
            tee.write("line\n")
        self.assertLess(time.time() - t0, 1.0)

    def test_writer_reopens_the_file_after_a_failed_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.log"
            path.write_text("", encoding="utf-8")
            tee = agent_logging._Tee(None, FailingOnce(), path)
            tee.write("after the hiccup\n")
            deadline = time.time() + 3
            while time.time() < deadline and "after the hiccup" not in path.read_text(encoding="utf-8"):
                time.sleep(0.05)
            text = path.read_text(encoding="utf-8")
            try:
                tee._handle.close()               # release the reopened file so the temp folder can go
            except Exception:
                pass
            self.assertIn("log file reopened", text)
            self.assertIn("after the hiccup", text)


if __name__ == "__main__":
    unittest.main()
