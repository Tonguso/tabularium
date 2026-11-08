"""Delegates make ambiguity visible (2026-10-10): progress saves are unconditional; genuine guesses still need an acceptance verdict."""
import sys, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
from tabularium.records import background_runtime as rt
from tabularium.core.roster import ROLES
from tabularium.delegation import delegation_contract as dc
class FirstCheckpoint(unittest.TestCase):
    def store(self, tmp, agent):
        folder = Path(tmp) / agent; folder.mkdir()
        store = rt.TaskStore(folder, ROLES[agent])
        with store.db() as db:
            db.execute("INSERT INTO tasks(id,chat,request,status,created,updated) VALUES (1,1,'r','running',0,0)")
        store.directory(1).mkdir(parents=True, exist_ok=True)
        return store

    def test_progress_can_be_saved_without_inventing_assumptions(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name in ("callimachus", "vitruvius"):
            store = self.store(tmp.name, name)
            store.checkpoint(1, 1, "saved file", "continue", [])
            store.checkpoint(1, 1, "saved file", "wait", [], blocker="QUESTION: which currency?")
            import json
            saved = json.loads((store.directory(1) / "checkpoint.json").read_text())
            self.assertEqual(saved["blocker"], "QUESTION: which currency?")

    def test_owner_task_is_not_gated(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self.store(tmp, "cato")
            s.checkpoint(1, 1, "started", "next", [])


class Acceptance(unittest.TestCase):
    def test_verdict_required_only_for_real_guesses(self):
        self.assertEqual(dc.guesses_refusal([], ""), "")
        self.assertEqual(dc.guesses_refusal(["none"], ""), "")
        self.assertIn("guesses_verdict", dc.guesses_refusal(["took 'global' to exclude HK"], ""))
        self.assertEqual(dc.guesses_refusal(["took 'global' to exclude HK"], "ok"), "")
        self.assertEqual(dc.guesses_refusal(["took 'global' to exclude HK"], "wrong: global includes HK here"), "")
        self.assertIn("guesses_verdict", dc.guesses_refusal(["x"], "fine I guess"))


if __name__ == "__main__":
    unittest.main()
