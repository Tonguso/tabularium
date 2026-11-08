"""A Claude Code update runs the self-test at once (Alex, 2026-10-22: [quote removed]). The CLI path the subscription route would use is checked every minute; a change (new version, or
none) triggers the self-test instead of waiting for the daily one."""
import contextlib, io, json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

from agent_harness import make_agent
from tabularium.models import subscription_runtime as bridge
class CliUpdate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / "cato"; root.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            self.agent = make_agent("cato", root)
        self.record = root / "task_state" / "claude_cli.json"

    def changed(self, path):
        with patch.object(bridge, "find_cli", return_value=path), contextlib.redirect_stdout(io.StringIO()):
            return self.agent._cli_changed()

    def test_first_sighting_records_without_triggering_then_each_change_triggers_once(self):
        self.assertFalse(self.changed(r"C:\x\claude-code\2.1.299\a\claude.exe"))      # first record: nothing to compare
        self.assertEqual(json.loads(self.record.read_text())["path"], r"C:\x\claude-code\2.1.299\a\claude.exe")
        self.assertFalse(self.changed(r"C:\x\claude-code\2.1.299\a\claude.exe"))      # same: no trigger
        self.assertTrue(self.changed(r"C:\x\claude-code\2.1.304\b\claude.exe"))       # an update: trigger
        self.assertFalse(self.changed(r"C:\x\claude-code\2.1.304\b\claude.exe"))      # once per change
        self.assertTrue(self.changed(None))                                          # it vanished: trigger too
        self.assertEqual(json.loads(self.record.read_text())["path"], "")


if __name__ == "__main__":
    unittest.main()
