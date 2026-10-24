"""'Work started' means an accepted start, resume or reopen in this turn; runtime work has its own lane (2026-10-21).

Cato's runtime report 2026-10-21-2242: at 22:01 he reopened task 262 (an accepted resume, queued) and the runtime
bounced his reply as an unbacked promise, because the check compared the highest task id before and after the
turn. The same evening a correction diagnosis held the single worker slot for half an hour while Alex's Tiro fix
(task 264) waited behind it."""
import tempfile, time, unittest
from pathlib import Path

from agent_harness import put
from background_runtime import TaskStore, meta
from agent_core.reply_envelope import Reply, unbacked
CHAT = 1


class Activation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = TaskStore(self.tmp.name)
        self.old = self.store.start(CHAT, "spreadsheet of managers")["id"]
        put(self.store, self.old, "finished")
        time.sleep(0.01)

    def test_a_new_start_backs_the_claim(self):
        t0 = time.time()
        self.store.start(CHAT, "new work")
        self.assertTrue(self.store.activated_since(CHAT, t0))

    def test_a_reopen_of_an_old_task_backs_the_claim_while_the_highest_id_stays(self):
        t0 = time.time()
        highest = self.store.newest(CHAT)
        self.store.control(CHAT, self.old, "resume", "add more managers")
        self.assertEqual(self.store.newest(CHAT), highest)                   # no duplicate task
        self.assertTrue(self.store.activated_since(CHAT, t0))
        self.assertFalse(unbacked(Reply("已重新打开这项任务，排队待执行。", work="started"), self.store.activated_since(CHAT, t0)))

    def test_a_failed_resume_status_only_and_earlier_tasks_do_not(self):
        with self.assertRaises(ValueError):
            self.store.control(CHAT, 999, "resume")
        self.store.status(CHAT)
        self.store.live(CHAT, self.old)
        t0 = time.time()
        self.assertFalse(self.store.activated_since(CHAT, t0))              # the old start was before this turn
        self.assertTrue(unbacked(Reply("I will add more managers.", work="started"), self.store.activated_since(CHAT, t0)))

    def test_another_chats_start_does_not_count(self):
        t0 = time.time()
        self.store.start(2, "someone else's work")
        self.assertFalse(self.store.activated_since(CHAT, t0))


class Lanes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = TaskStore(self.tmp.name)
        self.store.worker_limit = lambda: 1

    def running(self, request):
        ident = self.store.start(CHAT, request)["id"]
        put(self.store, ident, "running")
        return ident

    def test_a_running_diagnosis_does_not_block_owners_work(self):
        self.running("[Correction diagnosis] Alex corrected you at 22:01")
        tiro = self.store.start(CHAT, "Tiro: default subtitle and font size")["id"]
        claimed = self.store.claim_next()
        self.assertEqual(claimed["id"], tiro)

    def test_a_running_real_task_does_not_block_a_diagnosis_but_two_diagnoses_do_not_run_at_once(self):
        self.running("Build the basket")
        first = self.store.start(CHAT, "[Correction diagnosis] one")["id"]
        self.store.start(CHAT, "[Correction diagnosis] two")
        self.assertEqual(self.store.claim_next()["id"], first)
        self.assertIsNone(self.store.claim_next())                          # the meta lane is taken, the real slot too

    def test_owners_queued_work_goes_before_a_queued_diagnosis(self):
        self.store.start(CHAT, "[Trial lesson-x, current skill] rerun")
        real = self.store.start(CHAT, "Research the exits")["id"]
        self.assertEqual(self.store.claim_next()["id"], real)

    def test_meta_is_by_request_prefix(self):
        self.assertTrue(meta("[Eval case-3] rerun"))
        self.assertFalse(meta("Build a US AI basket"))


if __name__ == "__main__":
    unittest.main()
