"""Hung model calls (2026-10-10): the SDK stream has a silence watchdog; a worker past its deadline is stopped."""
import sys, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
from tabularium.models import subscription_runtime as sr
from tabularium.records import background_runtime as rt
class Silence(unittest.TestCase):
    def test_silent_only_when_idle_and_old(self):
        self.assertFalse(sr.stream_silent({"at": 1000.0, "busy": False}, 1000.0 + sr.SILENCE_SECONDS - 1))
        self.assertTrue(sr.stream_silent({"at": 1000.0, "busy": False}, 1000.0 + sr.SILENCE_SECONDS + 1))
        self.assertFalse(sr.stream_silent({"at": 1000.0, "busy": True}, 9999.0))      # a long tool call is not silence

    def test_partial_messages_requested(self):
        self.assertIn("include_partial_messages=True", (ROOT / "src" / "tabularium" / "models" / "subscription_runtime.py").read_text(encoding="utf-8"))


class Stalled(unittest.TestCase):
    def test_worker_stalled_rule(self):
        h = {"pid": 7, "phase": "model", "deadline": 2000.0}
        self.assertFalse(rt.worker_stalled(h, 2000.0 + rt.STALL_GRACE - 1))
        self.assertTrue(rt.worker_stalled(h, 2000.0 + rt.STALL_GRACE + 1))
        self.assertFalse(rt.worker_stalled({"phase": "model", "deadline": 1.0}, 9999.0))   # no pid, nothing to stop
        self.assertFalse(rt.worker_stalled({"pid": 7, "phase": "idle"}, 9999.0))            # no deadline


if __name__ == "__main__":
    unittest.main()
