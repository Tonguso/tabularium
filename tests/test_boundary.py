"""One boundary for every tool that takes a path (2026-11-12): the agent's own folder and its declared work roots.

Before: read_file and write_file took any path, so an agent could read the private agent's memory or the home's
.env, and a tainted turn could write the PowerShell profile or a skill file past the owner's gate."""
import contextlib, dataclasses, io, json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from agent_harness import make_agent
from tabularium.core.outcome import Refused


class Boundary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        (self.home / "coordinator").mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            self.agent = make_agent("coordinator", self.home / "coordinator")
        self.agent.BACKGROUND_TASK_ID = ""
        (self.home / "private").mkdir()
        (self.home / "private" / "memory.md").write_text("private", encoding="utf-8")
        (self.home / ".env").write_text("TELEGRAM_BOT_TOKEN=x", encoding="utf-8")
        self.work = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.work, ignore_errors=True))

    def roots(self, *roots):
        self.agent.config = dataclasses.replace(self.agent.config, work_roots=tuple(str(r) for r in roots))

    def test_other_agents_and_the_homes_own_files_are_never_readable(self):
        for path in (self.home / "private" / "memory.md", self.home / ".env", self.home / "POLICY.md"):
            out = self.agent.execute_tool("read_file", {"path": str(path)}, 1)
            self.assertIsInstance(out, Refused, path)
            self.assertIn("boundary", str(out))
        self.roots(self.home.parent)                                   # a work root above the home does not open the home
        self.assertIsInstance(self.agent.execute_tool("read_file", {"path": str(self.home / "private" / "memory.md")}, 1), Refused)

    def test_own_folder_is_open_except_memory_and_skills_for_writes(self):
        own = self.home / "coordinator"
        self.assertEqual(self.agent.execute_tool("write_file", {"path": str(own / "outputs" / "a.txt"), "content": "x"}, 1)[:1], "{")
        self.assertEqual(self.agent.execute_tool("read_file", {"path": str(own / "outputs" / "a.txt")}, 1), "x")
        for path in (own / "memory.md", own / "SKILL.md", own / "skills" / "briefs" / "SKILL.md"):
            out = self.agent.execute_tool("write_file", {"path": str(path), "content": "x"}, 1)
            self.assertIsInstance(out, Refused, path)
        (own / "skills" / "briefs").mkdir(parents=True)
        (own / "skills" / "briefs" / "SKILL.md").write_text("method", encoding="utf-8")
        self.assertEqual(self.agent.execute_tool("read_file", {"path": str(own / "skills" / "briefs" / "SKILL.md")}, 1), "method")

    def test_declared_work_roots_open_and_nothing_else_does(self):
        target = self.work / "report.txt"
        self.assertIsInstance(self.agent.execute_tool("write_file", {"path": str(target), "content": "x"}, 1), Refused)
        self.roots(self.work)
        self.assertEqual(self.agent.execute_tool("write_file", {"path": str(target), "content": "x"}, 1)[:1], "{")
        self.assertEqual(self.agent.execute_tool("read_file", {"path": str(target)}, 1), "x")
        self.assertIsInstance(self.agent.execute_tool("send_document", {"path": str(self.home / ".env")}, 1), Refused)
        self.assertIsInstance(self.agent.execute_tool("review_output", {"path": str(self.home / "private" / "memory.md")}, 1), Refused)

    def test_commands_run_without_the_users_profile(self):
        seen = []
        class Proc:
            pid, returncode = 1, 0
            def communicate(self, timeout=None):
                return b"ok", b""
        with patch("tabularium.core.agent_app.subprocess.Popen", side_effect=lambda argv, **kw: seen.append(argv) or Proc()):
            self.agent.execute_tool("execute_command", {"command": "Get-Date"}, 1)
        self.assertIn("-NoProfile", seen[0])
