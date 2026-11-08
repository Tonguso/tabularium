"""Cancel works on a task in any non-terminal state and says so when there is nothing to cancel (2026-10-12).

Task 143 sat in needs_attention; Alex said [quote removed] and "kill it", Cato called cancel twice, the store returned the
task unchanged without an error, Cato reported "killed", and the task stayed in the live list injected into every
message, so Cato kept bringing it up."""
import sys, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "cato")]
import background_runtime as rt


class CancelAnyState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.store = rt.TaskStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, ident, status):
        with self.store.db() as db:
            db.execute("INSERT INTO tasks(id,chat,request,status,created,updated) VALUES (?,1,'x',?,0,0)", (ident, status))

    def test_stopped_states_become_cancelled(self):
        for i, status in enumerate(("needs_attention", "interrupted", "needs_answer", "capped", "infeasible", "queued", rt.WAITING), 1):
            self.make(i, status)
            self.assertEqual(self.store.control(1, i, "cancel")["status"], "cancelled", status)

    def test_running_becomes_cancelling(self):
        self.make(20, "running")
        self.assertEqual(self.store.control(1, 20, "cancel")["status"], "cancelling")

    def test_terminal_states_report_nothing_to_cancel(self):
        self.make(30, "finished")
        task = self.store.control(1, 30, "cancel")
        self.assertEqual(task["status"], "finished")
        self.assertIn("nothing to cancel", task["summary"])


if __name__ == "__main__":
    unittest.main()
