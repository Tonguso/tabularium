"""A crash that will be rerun is never announced (2026-10-31), on a restart too (2026-11-14 14:38: a one-time recovery
from October copied every interrupted task into worker_failures when the store opened, so task 843's crash reached
Alex as 'stopped trying' and the task was rerun thirty seconds later)."""
import tempfile
import unittest
from pathlib import Path

from tabularium.records.background_runtime import TaskStore
WORK = [{"item": "Do what the brief asks", "check": "Read the result", "critical": True}]   # judged work is born with its contract


class InterruptedTasks(unittest.TestCase):
    def test_a_restart_does_not_announce_a_crash_that_will_be_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = TaskStore(root).start(1, "Inspect screenshot", "", completion_items=WORK)
            with TaskStore(root).db() as db:
                db.execute("UPDATE tasks SET status='interrupted', note='Worker error: RuntimeError' WHERE id=?", (task["id"],))
            notices = []
            TaskStore(root).report_worker_failures(lambda chat, text: notices.append(text))
            self.assertEqual(notices, [])


if __name__ == "__main__":
    unittest.main()
