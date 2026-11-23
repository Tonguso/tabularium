"""A process the model directs does not inherit the home's secrets (2026-11-23).

Every agent process loads the home's settings file into its environment, and until today execute_command, run_checks,
the output review and the agent's own scripts were started with that environment whole: each held every agent's bot
token, mail key and model key."""
import contextlib, io, os, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

from agent_harness import make_agent
from tabularium import paths


class Proc:
    pid, returncode = 1, 0

    def communicate(self, timeout=None):
        return b"ok", b""


class ToolEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / ".env").write_text("Fake_Bot_Token=abc\nFAKE_MODEL_KEY=def\n", encoding="utf-8")
        env = patch.dict(os.environ, {"TABULARIUM_HOME": str(self.home), "FAKE_BOT_TOKEN": "abc", "FAKE_MODEL_KEY": "def",
                                      "FAKE_ORDINARY": "kept"})
        env.start(); self.addCleanup(env.stop)

    def test_what_the_settings_file_defines_is_left_out_whatever_its_case_and_the_rest_stays(self):
        env = paths.tool_env()
        self.assertNotIn("FAKE_BOT_TOKEN", env)
        self.assertNotIn("FAKE_MODEL_KEY", env)
        self.assertEqual(env["FAKE_ORDINARY"], "kept")
        self.assertIn("PATH", {k.upper() for k in env})
        self.assertEqual(os.environ["FAKE_MODEL_KEY"], "def")               # the runtime itself keeps them

    def test_a_command_is_started_without_them(self):
        root = self.home / "coordinator"; root.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            agent = make_agent("coordinator", root)
        seen = {}
        with patch.dict(os.environ, {"TABULARIUM_HOME": str(self.home), "FAKE_MODEL_KEY": "def"}), \
             patch("tabularium.core.agent_app.subprocess.Popen", side_effect=lambda argv, **kw: seen.update(kw) or Proc()):
            agent.tool_execute_command("Get-ChildItem env:", 1)
        self.assertNotIn("FAKE_MODEL_KEY", seen["env"])
        self.assertIn("PATH", {k.upper() for k in seen["env"]})


if __name__ == "__main__":
    unittest.main()
