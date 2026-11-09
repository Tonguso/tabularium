"""A setting that could not be saved is logged, not swallowed (2026-10-15).

Why: voice mode, auto mode and the chat id were written inside `except Exception: pass`; a locked or full disk lost the
setting with no trace. Only OSError is handled now, and it is printed."""
import contextlib, io, unittest, tempfile
from pathlib import Path
from agent_harness import make_agent


class StateWrites(unittest.TestCase):
    def test_failed_save_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "coordinator"; root.mkdir()
            with contextlib.redirect_stdout(io.StringIO()):
                agent = make_agent("coordinator", root)
            missing = Path(tmp) / "no-such-dir" / "x.json"
            agent._voice_mode_path = lambda chat_id: missing
            agent._auto_mode_path = lambda chat_id: missing
            agent.CHAT_ID_FILE = missing
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                agent.set_voice_mode(1, "on"); agent.set_auto_mode(1, True); agent.save_chat_id(1)
            lines = out.getvalue().splitlines()
            self.assertEqual(len(lines), 3, lines)
            self.assertTrue(all(l.startswith("[state]") and "not saved" in l for l in lines), lines)
            self.assertEqual((agent.get_voice_mode(1), agent.get_auto_mode(1)), ("on", True))   # in-memory value still set


if __name__ == "__main__":
    unittest.main()
