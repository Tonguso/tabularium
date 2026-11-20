"""A release drains by pausing workers, not by waiting behind them (2026-11-14: one engineering worker held a release
for an hour, twice). The task is queued again once the worker has stopped, with its checkpoints."""
import tempfile
import time
import unittest
from pathlib import Path

from tabularium.records import lifecycle
from tabularium.records.background_runtime import TaskStore
from tabularium.records.lifecycle import InvalidTransition
WORK = [{"item": "Do what the brief asks", "check": "Read the result", "critical": True}]   # judged work is born with its contract


class DrainPauses(unittest.TestCase):
    def test_a_drain_pauses_running_work_and_the_stopped_worker_leaves_its_task_queued(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp))
            running = store.start(1, "Fix the GPU supervision", "", completion_items=WORK)
            queued = store.start(1, "Later work", "", completion_items=WORK)
            with store.db() as db:
                lifecycle.move(db, running["id"], "claim", "starting"); lifecycle.move(db, running["id"], "run", "running")
            self.assertEqual(store.pause_running(), [running["id"]])
            self.assertEqual(store.get(running["id"])["status"], "pausing")
            self.assertEqual(store.get(queued["id"])["status"], "queued")          # queued work is untouched
            with store.db() as db:                                                 # the worker has stopped: queued again
                lifecycle.move(db, running["id"], "paused", "queued", "Paused for a release; resumes on the new build.")
            self.assertEqual(store.get(running["id"])["status"], "queued")
            with store.db() as db:
                with self.assertRaises(InvalidTransition):                         # a queued task is not pausable
                    lifecycle.move(db, queued["id"], "pause", "pausing")


if __name__ == "__main__":
    unittest.main()
