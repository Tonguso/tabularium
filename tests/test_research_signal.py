"""The research-quality signal reruns real cases and reports closer / same / further, never a pass (2026-10-24)."""
import json, tempfile, unittest
from pathlib import Path

from agent_harness import make_agent, put
from tabularium.learning import research_signal
from tabularium.learning import regression
from tabularium.learning import trials
class Signal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / "cato"; root.mkdir()
        self.agent = make_agent("cato", root)
        self.store = self.agent._background
        self.root = root
        cases = [{"id": "task-7", "source_task": 7, "request": "Research US towers and write a note", "context": "",
                  "reference": "Towers note: three names, evidence cited.", "task_type": "research"},
                 {"id": "task-8", "source_task": 8, "request": "Build a basket workbook", "context": "",
                  "reference": "Workbook built.", "task_type": "basket_build"}]
        regression.cases_path(root).parent.mkdir(parents=True, exist_ok=True)
        regression.cases_path(root).write_text(json.dumps(cases), encoding="utf-8")

    def test_research_candidates_are_chosen_and_reruns_are_isolated_with_normal_tools(self):
        chosen = research_signal.candidates(self.root, "research")
        self.assertEqual([c["id"] for c in chosen], ["task-7"])
        ident = research_signal.start(self.store, 1, chosen[0])
        ctx = json.loads((self.store.directory(ident) / "context.json").read_text(encoding="utf-8"))
        self.assertTrue(trials.isolated(ctx))                                   # nothing sent, nothing delegated
        self.assertIsNone((ctx.get("eval") or {}).get("benchmark"))              # not a benchmark: tools stay normal
        self.assertFalse(ctx["notify_telegram"])
        self.assertTrue(self.store.get(ident)["request"].startswith("[Eval signal task-7"))

    def test_a_run_records_counts_and_the_dashboard_summary_flags_three_further_runs(self):
        answers = iter(["A thinner note with one name.", "Towers note: three names, evidence cited, plus a fourth.", ""])
        def starter(store, chat, case):
            ident = research_signal.start(store, chat, case)
            (store.directory(ident) / "result.txt").write_text(next(answers), encoding="utf-8")
            put(store, ident, "finished", "")
            return ident
        def judge(prompt, schema):
            return json.dumps({"better": "earlier" if "thinner" in prompt else "new", "reason": "names"})
        waiter = lambda s, i: s.get(i)["status"]
        r1 = research_signal.run(self.root, self.store, 1, judge, sample=1, starter=starter, waiter=waiter, log=lambda *a: None)
        self.assertEqual(r1["counts"], {"closer": 0, "same": 0, "further": 1})
        self.assertIn("A signal, not a score", research_signal.table(r1))
        r2 = research_signal.run(self.root, self.store, 1, judge, sample=1, starter=starter, waiter=waiter, log=lambda *a: None)
        self.assertEqual(r2["counts"]["closer"], 1)
        r3 = research_signal.run(self.root, self.store, 1, judge, sample=1, starter=starter, waiter=waiter, log=lambda *a: None)
        self.assertIn("no answer", r3["cases"][0]["reason"])
        s = research_signal.latest(self.root)
        self.assertEqual((s["runs"], s["further_runs"], s["drifting"]), (3, 2, False))

    def test_an_unverified_prose_rerun_is_compared_on_its_text_and_closed(self):
        # 2026-10-24 first run: all three reruns ended needs_attention (no declared checks) and were counted "further" unread
        def starter(store, chat, c):
            ident = research_signal.start(store, chat, c)
            (store.directory(ident) / "result.txt").write_text(
                "Completion not verified: Requirement 1 x: unverified.\n\nTowers note: three names, evidence cited.\n[Route] claude 3 turns",
                encoding="utf-8")
            put(store, ident, "needs_attention", "Completion: needs_attention")
            return ident
        seen = {}
        def judge(prompt, schema):
            seen["prompt"] = prompt; return json.dumps({"better": "equal", "reason": "equal"})
        r = research_signal.run(self.root, self.store, 1, judge, sample=1, starter=starter, waiter=lambda s, i: s.get(i)["status"], log=lambda *a: None)
        self.assertEqual(r["counts"]["same"], 1)
        self.assertNotIn("Completion not verified", seen["prompt"]); self.assertNotIn("[Route]", seen["prompt"])
        self.assertEqual(self.store.get(r["cases"][0]["task"])["status"], "cancelled")   # read, then closed; no attention needed

    def test_a_judge_failure_is_recorded_as_same_not_a_crash(self):
        def judge(prompt, schema):
            raise RuntimeError("400")
        c = research_signal.compare(judge, {"request": "r", "reference": "ref"}, "an answer")
        self.assertEqual(c["closeness"], "same"); self.assertTrue(c["judge_failed"])


if __name__ == "__main__":
    unittest.main()
