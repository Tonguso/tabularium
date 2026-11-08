"""execute_command refuses any process kill not aimed at literal PIDs, in every mode, on the real agent.
On 2026-10-06 23:22 Cato's background cleanup 'Get-Process python | ? {StartTime > -40 min} | Stop-Process' killed all
four agents (restarted 36 minutes earlier) and its own worker. Rewritten 2026-10-14 from a source-inspection test."""
import sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from agent_harness import make_agent

FATAL = (r"Get-Process python -ErrorAction SilentlyContinue | ? {$_.StartTime -gt (Get-Date).AddMinutes(-40)} "
         r"| Stop-Process -ErrorAction SilentlyContinue; Remove-Item -Recurse -Force D:\Agents\cato\scratch\jls; "
         r'"cleaned"')
REFUSED = [
    FATAL,
    "Stop-Process -Name python",
    "Stop-Process -ProcessName python -Force",
    "Get-Process | ? {$_.CPU -gt 100} | Stop-Process",
    "gps python | kill",
    "kill -Name python",
    "taskkill /F /IM python.exe",
    "taskkill /IM python.exe /T",
    "Stop-Process -Id $pid",
    "Stop-Process -InputObject $p",
    "pkill -f telegram.py",
    "echo ok; Stop-Process -Name python",
]
ALLOWED = [
    "Stop-Process -Id 29116 -ErrorAction SilentlyContinue",
    "Stop-Process 29116",
    "Stop-Process -Id 29116,29117 -Force",
    "taskkill /PID 29116 /T /F",
    "taskkill /F /PID 29116",
    "kill 29116",
    r'Get-Content seg.err -Tail 2; Stop-Process -Id 29116 -ErrorAction SilentlyContinue; cmd /c "start /b python seg.py"',
    "python -c \"import subprocess; p=subprocess.Popen(['x']); p.kill()\"",
    "Get-Process python | Select Id,StartTime",
    r"python D:\Agents\cato\scripts\build_basket.py",
    r"Remove-Item -Recurse -Force D:\Agents\cato\scratch\jls",
]


class ProcessKillGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name) / "cato"; root.mkdir()
        cls.agent = make_agent("cato", root)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_kills_by_name_or_filter_are_recognised(self):
        for cmd in REFUSED:
            self.assertTrue(self.agent._kills_by_name(cmd), f"should refuse {cmd!r}")

    def test_literal_pid_kills_and_ordinary_commands_pass(self):
        for cmd in ALLOWED:
            self.assertFalse(self.agent._kills_by_name(cmd), f"should allow {cmd!r}")

    def test_refused_in_every_mode_and_never_run(self):
        """In the foreground with /auto on (which runs anything else unconfirmed) and in a background worker."""
        agent = self.agent
        with patch("subprocess.run") as run, patch("subprocess.Popen") as popen:
            agent.BACKGROUND_TASK_ID = ""
            agent.auto_modes[1] = True
            self.assertEqual(agent.tool_execute_command(FATAL, 1), agent.KILL_REFUSAL)
            agent.BACKGROUND_TASK_ID = "1"
            self.assertEqual(agent.tool_execute_command(FATAL, 1), agent.KILL_REFUSAL)
            run.assert_not_called(); popen.assert_not_called()
        self.assertIn("literal PIDs", agent.KILL_REFUSAL)


if __name__ == "__main__":
    unittest.main()
