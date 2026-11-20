"""A task is judged against the contract it is born with, so judged work cannot be born without one (2026-11-20).

Cato's receiving task for a hand-off to Cal or Vit was created with no completion items, while completion requires a
nonempty contract: task 989 ("Receive Cal's work for Alex: Leveraged ETF ...") could never finish, whatever Cal
delivered. The store now refuses such a task; only the runtime's own records (trials, evaluation reruns, correction
diagnoses, method reviews) carry no contract, and they end as records, not as unverified work."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from tabularium.records import verification
from tabularium.records.background_runtime import TaskStore
from tabularium.records.task_decisions import finish

ITEM = [{"item": "The report answers the question", "check": "Read it", "critical": True}]


class Contract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = TaskStore(Path(self.tmp.name))

    def test_judged_work_cannot_start_without_its_contract(self):
        for items in (None, []):
            with self.assertRaisesRegex(ValueError, "completion_items"):
                self.store.start(1, "Research the ETF rebalancing flow", completion_items=items)
            with self.assertRaisesRegex(ValueError, "completion_items"):
                self.store.start(1, "Receive Cal's work", parked_on={"agent": "cal", "request_id": "r1"}, completion_items=items)
        task = self.store.start(1, "Research the ETF rebalancing flow", completion_items=ITEM)
        self.assertEqual(verification.saved_items(self.store, task["id"])[0]["item"], ITEM[0]["item"])

    def test_the_runtimes_own_records_need_none_and_end_as_records(self):
        rows = [self.store.start(1, "[Eval x attempt 1] Do it", extra={"eval": {"case": "x", "signal": True}}),
                self.store.start(1, "[Trial t] Do it", extra={"trial": {"key": "t"}}),
                self.store.start(1, "[Correction diagnosis] Recorded feedback")]
        for row in rows:
            ident = self.store.claim_next()["id"]
            self.assertEqual(finish(self.store, ident, NS(task_outcome="completed", complete=True, question="")), "finished")
        self.assertEqual(len(rows), 3)


if __name__ == "__main__":
    unittest.main()
