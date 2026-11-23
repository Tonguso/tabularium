"""A task's history is appended by the move that changes its status (2026-11-23).

The task row kept only its latest status and note. The completion verdict lived in completion.json, written before
the move committed and never touched when the task was resumed, so the dashboard showed a finished verdict on a task
that was running again; the time it parked lived in context.json; activations had a table of their own. Now each
move appends an event in its own transaction, and those facts are read from the events."""
import json, sqlite3, tempfile, time, unittest

from agent_harness import put
from tabularium.core.reply_envelope import Reply
from tabularium.records import lifecycle
from tabularium.records.background_runtime import TaskStore, WAITING
from tabularium.records.lifecycle import InvalidTransition
WORK = [{"item": "Do what the brief asks", "check": "Read the result", "critical": True}]
CHAT = 1


class Journal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = TaskStore(self.tmp.name)
        self.task = self.store.start(CHAT, "Build the report", "", completion_items=WORK)["id"]

    def ops(self, ident=None):
        return [(e["op"], e["status"]) for e in self.store.history(ident or self.task)]

    def test_a_task_is_born_with_its_first_event_and_every_move_appends_one(self):
        self.assertEqual(self.ops(), [("start", "queued")])
        self.store.move(self.task, "claim", "starting")
        self.store.move(self.task, "run", "running")
        self.store.move(self.task, "stop", "needs_attention", "Deadline passed")
        self.assertEqual(self.ops(), [("start", "queued"), ("claim", "starting"), ("run", "running"), ("stop", "needs_attention")])
        last = self.store.history(self.task)[-1]
        self.assertEqual(last["note"], "Deadline passed")
        self.assertEqual(self.store.get(self.task)["status"], last["status"])   # the row is the newest event

    def test_a_refused_move_appends_nothing(self):
        with self.assertRaises(InvalidTransition):
            self.store.move(self.task, "reopen", "queued")                      # reopen applies to finished tasks only
        self.assertFalse(self.store.move(self.task, "wake", "queued", quiet=True))
        self.assertEqual(self.ops(), [("start", "queued")])

    def test_the_completion_verdict_is_the_finish_events_data_and_ends_when_the_task_moves_on(self):
        put(self.store, self.task, "running")
        state = self.store.finish(self.task, Reply("Done.", task_outcome="completed"), notes={"route": "test-route"})
        verdict = self.store.completion(self.task)
        self.assertEqual(verdict["state"], state)
        self.assertEqual(verdict["route"], "test-route")
        self.assertIn("reasons", verdict)
        folder = self.store.directory(self.task)
        self.assertFalse((folder / "completion.json").exists())                 # no second copy
        self.assertFalse((folder / "parked_answer.txt").exists())
        put(self.store, self.task, "finished")
        self.store.control(CHAT, self.task, "resume", "Add the Japan names")    # reopened: the old verdict no longer stands
        self.assertIsNone(self.store.completion(self.task))
        self.assertEqual(self.ops()[-1], ("reopen", "queued"))

    def test_work_started_is_read_from_the_events(self):
        later = time.time() + 5
        self.assertTrue(self.store.activated_since(CHAT, time.time() - 60))     # the start
        self.assertFalse(self.store.activated_since(CHAT, later))
        self.assertFalse(self.store.activated_since(2, 0))                      # another chat's work is not this chat's
        put(self.store, self.task, "needs_attention")
        self.store.control(CHAT, self.task, "resume", "Go on")
        with self.store.db() as db:                                              # a claim is not an activation
            lifecycle.move(db, self.task, "claim", "starting")
            newest = [r[0] for r in db.execute("SELECT op FROM events WHERE task=? ORDER BY id DESC LIMIT 2", (self.task,))]
        self.assertEqual(newest, ["claim", "resume"])

    def test_a_task_born_waiting_knows_when_it_parked(self):
        before = time.time()
        task = self.store.start(CHAT, "Receive Vit's work", "", completion_items=WORK,
                                parked_on={"agent": "vit", "request_id": "r1", "purpose": "fixes"})["id"]
        with self.store.db() as db:
            parked = lifecycle.since(db, task, WAITING)
        self.assertGreaterEqual(parked, before)
        context = json.loads((self.store.directory(task) / "context.json").read_text(encoding="utf-8"))
        self.assertNotIn("parked_at", context)

    def test_a_task_from_before_the_journal_is_adopted_once_with_the_verdict_its_folder_held(self):
        with self.store.db() as db:
            old = db.execute("INSERT INTO tasks(chat,request,status,created,updated,note) VALUES (1,'Old work','finished',100,200,'Completion: finished; ')").lastrowid
        folder = self.store.directory(old); folder.mkdir(exist_ok=True)
        (folder / "completion.json").write_text(json.dumps({"passed": True, "verdict": "passed", "reasons": [], "state": "finished"}), encoding="utf-8")
        self.assertEqual(self.store.history(old), [])
        again = TaskStore(self.tmp.name)                                         # the store starts: the old task is adopted
        self.assertEqual([(e["op"], e["status"], e["at"]) for e in again.history(old)], [("adopt", "finished", 200)])
        self.assertTrue(again.completion(old)["passed"])
        TaskStore(self.tmp.name)                                                 # and only once
        self.assertEqual(len(again.history(old)), 1)
        self.assertEqual(self.ops(), [("start", "queued")])                      # a journaled task is left alone


if __name__ == "__main__":
    unittest.main()
