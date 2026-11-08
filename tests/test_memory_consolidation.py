"""Memory clean-ups are a candidate that is never live until the exam has run on it and Alex said go (2026-10-20)."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tabularium.memory import memory_consolidation as mc

from tabularium.memory import memory_context

from tabularium.learning import trials
class Consolidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "task_state").mkdir()
        (self.root / "memory.md").write_text("# Core\n- Alex likes tea.\n- Alex likes tea a lot.\n- Rent is due on the 1st.\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_propose_edits_candidate_not_live(self):
        self.assertEqual(mc.propose(self.root, "- Alex likes tea a lot.\n", "", "duplicate"), 1)
        self.assertIn("tea a lot", (self.root / "memory.md").read_text(encoding="utf-8"))
        self.assertNotIn("tea a lot", (self.root / "task_state" / "memory_candidate.md").read_text(encoding="utf-8"))
        with self.assertRaises(ValueError):
            mc.propose(self.root, "- Alex likes tea a lot.\n", "")        # gone from the candidate already
        self.assertIn("-- Alex likes tea a lot.", mc.diff(self.root))

    def test_apply_needs_trial_then_replaces_and_clears(self):
        mc.propose(self.root, "- Alex likes tea a lot.\n", "")
        with self.assertRaises(ValueError):
            mc.apply(self.root)
        meta = self.root / "task_state" / "memory_candidate.json"
        record = json.loads(meta.read_text(encoding="utf-8"))
        record["trial"] = {"before": {"correct": 1, "partial": 0, "wrong": 2, "error": ""}, "after": {"correct": 3, "partial": 0, "wrong": 0, "error": ""}, "items": []}
        meta.write_text(json.dumps(record), encoding="utf-8")
        self.assertIn("live 1/0/2", mc.summary(self.root))
        done = mc.apply(self.root)
        self.assertEqual(done["edits"], 1)
        self.assertNotIn("tea a lot", (self.root / "memory.md").read_text(encoding="utf-8"))
        self.assertFalse(meta.exists())
        self.assertEqual(mc.summary(self.root), "No memory candidate.")

    def test_trial_runs_exam_on_live_then_candidate_same_questions(self):
        mc.propose(self.root, "- Alex likes tea a lot.\n", "")
        seen = []
        def run(runtime, chat, sources=None):
            text = memory_context.memory_path(self.root).read_text(encoding="utf-8")
            seen.append(("candidate" if "tea a lot" not in text else "live", [s["id"] for s in sources]))
            return {"correct": 1, "partial": 0, "wrong": 0, "items": [{"question": "Tea?", "score": "correct"}], "error": ""}
        runtime = SimpleNamespace(AGENT_DIR=self.root, _recall=None)
        picked = [{"id": 1, "text": "x", "said_at": "2026-10-19"}]
        original = mc.memory_exam.pick_sources
        mc.memory_exam.pick_sources = lambda recall, chat, seed=None, **kw: picked
        try:
            t = mc.trial(runtime, 1, run=run)
        finally:
            mc.memory_exam.pick_sources = original
        self.assertEqual(seen, [("live", [1]), ("candidate", [1])])
        self.assertEqual(t["items"], [{"question": "Tea?", "before": "correct", "after": "correct"}])
        self.assertEqual(memory_context.memory_path(self.root), self.root / "memory.md")   # override released
        self.assertFalse(trials.LOCAL)
        with self.assertRaises(ValueError):
            mc.propose(self.root, "- Rent", "- Rent")                                      # trialled: no more edits


if __name__ == "__main__":
    unittest.main()
