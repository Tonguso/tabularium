"""Lessons That Change Methods, step 3: a case declares fixtures and the trial worker provides them without side
effects; the shared lesson guidance is in every learning role's prompt; the dashboard carries the new states."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_harness import make_agent
from tabularium.learning import regression, trials

FIXTURES = {"files": [{"path": "inputs/members.csv", "content": "name,weight\nA,0.5\nB,0.5\n"}],
            "delegate": {"result": "Evidence pack: two names, both verified."},
            "verify": {"exit_code": 1, "output": "weights sum to 1.1"},
            "search": [{"title": "Filing", "url": "https://example.com/10k", "snippet": "revenue 12.3bn"}]}


def case(fixtures=None):
    return dict(id="c1", version=1, request="Build it.", oracle=dict(basis="owner", reviewed_by="test"),
                requirements=[dict(id="r", check=dict(kind="text", path="result.txt", expected="x"))],
                **({"fixtures": fixtures} if fixtures is not None else {}))


class Fixtures(unittest.TestCase):
    def test_a_case_declares_only_the_four_fixture_kinds_with_safe_paths(self):
        regression.validate_case(case())
        regression.validate_case(case(FIXTURES))
        for bad in ({"shell": "rm -rf"}, {"files": [{"path": "../x", "content": ""}]}, {"files": [{"path": "C:/x", "content": ""}]},
                    {"delegate": {}}, {"verify": {"exit_code": "0"}}, {"search": "text"}):
            with self.assertRaises(ValueError):
                regression.validate_case(case(bad))

    def test_the_worker_gets_the_files_the_scripted_tools_and_nothing_else(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ):
            for name in (trials.READS_ENV, trials.TRIAL_DIR_ENV, trials.OVERRIDES_ENV):
                os.environ.pop(name, None)
            root = Path(tmp) / "coordinator"; root.mkdir()
            skill = root / "skill.md"; skill.write_text("# s", encoding="utf-8")
            trial = dict(cohort="c", skill=str(skill), skill_sha=trials.learning._sha(skill), overrides={}, max_steps=5,
                         system_prompt="Do the task.", fixtures=FIXTURES)
            trials.prepare_worker({"trial": trial}, tmp)
            inputs = Path(tmp) / "trial_output" / "inputs" / "inputs" / "members.csv"
            self.assertEqual(inputs.read_text(encoding="utf-8"), FIXTURES["files"][0]["content"])
            self.assertIsNone(trials.read_refusal(inputs))                                   # inside the run's folder
            self.assertIsNotNone(trials.read_refusal(root / "other.csv"))
            self.assertIn(str(inputs), trials.worker_prompt({"trial": trial}))
            with contextlib.redirect_stdout(io.StringIO()):
                agent = make_agent("coordinator", root)
            trials.scope(agent, {"trial": trial})
            self.assertEqual(agent._tool_scope, {"read_file", "write_file", "delegate", "execute_command", "web_search"})
            self.assertEqual(json.loads(agent.execute_tool("delegate", {"action": "submit", "agent": "cal", "brief": "b"}, 1))["status"], "submitted")
            self.assertEqual(json.loads(agent.execute_tool("delegate", {"action": "result", "request_id": "scripted-1"}, 1))["text"], FIXTURES["delegate"]["result"])
            out = str(agent.execute_tool("execute_command", {"command": "python check.py"}, 1))
            self.assertIn("exit code 1", out); self.assertIn("weights sum to 1.1", out)
            self.assertIn("revenue 12.3bn", str(agent.execute_tool("web_search", {"query": "10-K"}, 1)))
            self.assertIn("not available here", str(agent.execute_tool("send_email", {"to": "x"}, 1)))
            trials.scope(agent, {"trial": dict(trial, fixtures={})})
            self.assertEqual(agent._tool_scope, {"read_file", "write_file"})                 # no fixture, no tool
            self.assertIsNone(trials.scripted(agent, "execute_command", {"command": "x"}))

    def test_every_learning_role_carries_the_one_lesson_guidance_and_the_private_role_does_not(self):
        from tabularium.core.roster import ROLES
        with tempfile.TemporaryDirectory() as tmp:
            for slot, role in ROLES.items():
                root = Path(tmp) / slot; root.mkdir()
                with contextlib.redirect_stdout(io.StringIO()):
                    agent = make_agent(slot, root)
                self.assertEqual("Lessons: a change of method, not a reminder" in agent.SYSTEM_PROMPT, bool(role.learns), slot)
                self.assertEqual("lesson(action='checked'" in agent.SYSTEM_PROMPT, bool(role.learns), slot)


if __name__ == "__main__":
    unittest.main()
