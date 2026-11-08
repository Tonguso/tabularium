"""The Claude route finds the Claude Code CLI bundled with the desktop app, and says so when it finds nothing (2026-10-22).

2026-10-20 20:14 an update removed the stand-alone claude.exe (~/.local/bin and the SDK bundle). find_cli found no
candidate, every turn raised "Claude Code is not installed", and the router fell back to the ChatGPT subscription
for two days without telling Alex: cooldowns of five minutes, retried forever, no notice (only quota and sign-out
were announced). Alex: [quote removed]."""
import os, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from tabularium.models import subscription_runtime as bridge
from tabularium.models import provider_routing as routing
class DesktopAppCli(unittest.TestCase):
    def test_find_cli_uses_the_desktop_apps_claude_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "Claude-Data" / "claude_abc" / "claude-code" / "2.1.300" / "aaaa" / "claude.exe"
            new = Path(tmp) / "Claude-Data" / "claude_abc" / "claude-code" / "2.1.304" / "bbbb" / "claude.exe"
            for exe in (old, new):
                exe.parent.mkdir(parents=True); exe.write_bytes(b"MZ")
            os.utime(old, (1, 1))
            with patch.object(bridge.shutil, "which", return_value=None), patch.object(bridge.Path, "home", return_value=Path(tmp)), \
                 patch.dict(os.environ, {"APPDATA": tmp, "LOCALAPPDATA": tmp}):
                import claude_agent_sdk
                with patch.object(claude_agent_sdk, "__file__", str(Path(tmp) / "sdk" / "__init__.py")):
                    self.assertEqual(bridge.find_cli(), str(new))            # the newest build wins
            new.unlink(); old.unlink()
            with patch.object(bridge.shutil, "which", return_value=None), patch.object(bridge.Path, "home", return_value=Path(tmp)), \
                 patch.dict(os.environ, {"APPDATA": tmp, "LOCALAPPDATA": tmp}):
                with patch.object(claude_agent_sdk, "__file__", str(Path(tmp) / "sdk" / "__init__.py")):
                    self.assertIsNone(bridge.find_cli())


class MissingCliIsAnnounced(unittest.TestCase):
    def test_owner_is_told_once_and_the_fallback_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = NS(AGENT_DIR=root, RUNTIME_DIR=root, _review_counts={}, _strip_old_images=Mock(),
                       OPENAI_API_KEY="fake", ANTHROPIC_API_KEY="", send_message=Mock(),
                       run_turn_anthropic=Mock(), run_turn_openai=Mock(return_value="OpenAI answer"))
            routing.save(root, {"enabled": True, "routes": [{"kind": "anthropic_subscription", "model": "claude-test"},
                                                             {"kind": "openai_subscription", "model": "gpt-test"}]})
            with patch.object(bridge, "run_claude", side_effect=routing.Unavailable("Claude Code is not installed (no claude.exe found)")):
                self.assertEqual(routing.run(agent, [], 1), "OpenAI answer")
                self.assertEqual(routing.run(agent, [], 1), "OpenAI answer")
            agent.send_message.assert_called_once()
            notice = agent.send_message.call_args.args[1]
            self.assertIn("Claude subscription is unavailable", notice)
            self.assertIn("not installed", notice)
            self.assertIn("fallback", notice)
            block = routing.read(root / "task_state" / "route_anthropic_subscription.json")
            self.assertIn("not installed", block["reason"])
            self.assertTrue(block["notified"])


if __name__ == "__main__":
    unittest.main()
