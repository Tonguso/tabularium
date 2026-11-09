"""Vit's run_checks tool runs a check with a real timeout and returns a structured verdict (real Vit agent)."""
import json, sys, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
from agent_harness import make_agent


class RunChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name) / "engineer"; root.mkdir()
        cls.vit = make_agent("engineer", root)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_compile_and_pytest_report_structured_results(self):
        r = json.loads(self.vit.tool_run_checks("compile", str(ROOT), "task_cost.py", timeout=120))
        self.assertEqual(r["exit"], 0); self.assertTrue(r["passed"]); self.assertFalse(r["timed_out"])
        r = json.loads(self.vit.tool_run_checks("pytest", str(ROOT), "tests/test_context_archive.py", timeout=180))
        self.assertTrue(r["passed"], str(r)[-500:])
        self.assertRegex(r["summary"], r"\d+ passed")

    def test_bad_kind_and_bad_cwd_are_errors_not_exceptions(self):
        self.assertIn("unknown kind", self.vit.tool_run_checks("lint", str(ROOT)))
        self.assertIn("not a directory", self.vit.tool_run_checks("pytest", str(ROOT / "nope")))

    def test_the_tool_is_offered_and_dispatched(self):
        names = {t.get("name") for t in self.vit.get_tools("anthropic")}
        self.assertIn("run_checks", names)
        out = self.vit._execute_tool_unguarded("run_checks", {"kind": "lint", "cwd": str(ROOT)}, 1)
        self.assertIn("unknown kind", out)


if __name__ == "__main__":
    unittest.main()
