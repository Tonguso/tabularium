"""Submitting background work starts it before the reply (2026-10-11): the summary says what happened, in order."""
import sys, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
from tabularium.records import background_runtime as rt
class StartNow(unittest.TestCase):
    def test_without_a_monitor_it_only_queues(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = rt.TaskStore(Path(tmp) / "cato")
            store.launch = lambda row: self.fail("must not launch without a monitor")
            task = store.start(1, "job", "ctx")
            self.assertEqual(task["status"], "queued")
            self.assertTrue(task["summary"].startswith("queued:"))

    def test_started_or_queued_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = rt.TaskStore(Path(tmp) / "cato")
            launched = []
            store.launch = lambda row: launched.append(row["id"])          # no real worker process in a test
            store.monitoring = True                                          # as in a serving agent process
            first = store.start(1, "first job", "ctx")
            self.assertEqual(launched, [first["id"]])
            self.assertTrue(first["summary"].startswith("Launch requested"), first["summary"])
            self.assertEqual(store.get(first["id"])["status"], "starting")
            second = store.start(1, "second job", "ctx")
            self.assertEqual(launched, [first["id"]])                        # one worker slot for Cato
            self.assertIn(f"waits behind task {first['id']}", second["summary"])
            self.assertEqual(store.get(second["id"])["status"], "queued")


if __name__ == "__main__":
    unittest.main()
