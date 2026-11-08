"""Offline: a background worker with a delegate in flight must park, not sleep/poll."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
from tabularium.records.background_runtime import TaskStore, IN_FLIGHT_REFUSAL, STATUS_REPEAT_SECONDS
from agent_harness import make_agent


REFUSAL = ("A delegate is in flight. Save task_checkpoint and end your turn; "
           "the runtime parks you and resumes when the result returns.")


class NoPollWait(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_refusal_text_exact(self):
        self.assertEqual(IN_FLIGHT_REFUSAL, REFUSAL)

    def test_refuses_waits_and_other_agent_state_while_in_flight(self):
        self.store.add_delegation(7, "cal", "req-1", "research", "worker_judgment")
        for cmd in ["Start-Sleep 55; gc D:\\Agents\\cal\\task_state\\background\\3\\checkpoint.json",
                    "Start-Sleep -Seconds 58", "sleep 30", "timeout /t 60", "ping -n 60 127.0.0.1",
                    "Wait-Process -Id 123", "Wait-Event", "python -c \"import time; time.sleep(50)\"",
                    "[System.Threading.Thread]::Sleep(5000)",
                    "python D:\\Agents\\cato\\scripts\\delegate_local.py cal status --request-id req-1",
                    "Get-Content D:\\Agents\\cal\\health.json",
                    "gc 'D:/Agents/vitruvius/task_state/background/8/checkpoint.json'",
                    "$p='D:\\Agents\\cal'; Get-ChildItem \"$p\\task_state\""]:
            self.assertEqual(self.store.command_refusal(7, cmd), REFUSAL, cmd)

    def test_ordinary_commands_still_run_while_in_flight(self):
        self.store.add_delegation(7, "vit", "req-2", "build", "worker_judgment")
        for cmd in ["Get-ChildItem D:\\Agents\\cato\\reports", "python scripts\\build.py",
                    "gc D:\\Agents\\cato\\task_state\\background\\7\\checkpoint.json",
                    "gc D:\\Agents\\cal\\reports\\summary.md"]:
            self.assertIsNone(self.store.command_refusal(7, cmd), cmd)

    def test_no_refusal_without_in_flight_delegate(self):
        cmd = "Start-Sleep 55; gc D:\\Agents\\cal\\health.json"
        self.assertIsNone(self.store.command_refusal(7, cmd))  # none attached
        self.store.add_delegation(7, "cal", "req-3", "research", "worker_judgment")
        self.store.mark_delegation(7, "req-3", "returned")
        self.assertIsNone(self.store.command_refusal(7, cmd))  # returned: not in flight
        self.store.add_delegation(8, "cal", "req-4", "research", "worker_judgment")
        self.assertIsNone(self.store.command_refusal(7, cmd))  # another task's delegate

    def test_repeated_status_refused_within_window(self):
        t0 = 1_000_000.0
        self.assertIsNone(self.store.status_refusal(7, "req-1", now=t0))
        self.assertEqual(self.store.status_refusal(7, "req-1", now=t0 + 60), REFUSAL)
        self.assertIsNone(self.store.status_refusal(7, "req-other", now=t0 + 60))
        self.assertIsNone(self.store.status_refusal(7, "req-1", now=t0 + STATUS_REPEAT_SECONDS + 1))
        self.assertEqual(STATUS_REPEAT_SECONDS, 300)

    def test_park_instruction_forbids_waiting(self):
        self.store.add_delegation(7, "cal", "req-1", "research", "worker_judgment")
        text = self.store.delivery_block(7)
        for phrase in ("Start-Sleep", "status calls", "other agents' task_state", "task_checkpoint", "end your turn"):
            self.assertIn(phrase, text)


class OnlyWorkersAreRefused(unittest.TestCase):
    """The no-poll refusals apply inside a background worker and nowhere else (real Cato agent; rewritten 2026-10-14
    from a syntax-tree inspection of the source)."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "cato"; root.mkdir()
        self.agent = make_agent("cato", root)
        self.asked = []

    def tearDown(self):
        self.tmp.cleanup()

    def test_execute_command_consults_the_refusal_only_in_a_worker(self):
        self.agent._background.command_refusal = lambda ident, command: self.asked.append(command) or "REFUSED"
        with patch("subprocess.run") as run, patch("subprocess.Popen") as popen:
            self.agent.BACKGROUND_TASK_ID = "1"
            self.assertEqual(self.agent.tool_execute_command("Start-Sleep 60", 1), "REFUSED")
            run.assert_not_called(); popen.assert_not_called()            # refused before anything is launched
        self.agent.BACKGROUND_TASK_ID = ""
        self.asked.clear()
        self.agent._background.command_refusal = lambda ident, command: self.fail("foreground commands are never refused")
        with patch("subprocess.Popen") as popen:
            popen.return_value.communicate.return_value = (b"ok", b""); popen.return_value.returncode = 0
            self.agent.tool_execute_command("echo ok", 1)

    def test_delegate_status_is_throttled_only_for_a_worker(self):
        self.agent._background.status_refusal = lambda ident, request_id: self.asked.append(request_id) or IN_FLIGHT_REFUSAL
        self.agent.BACKGROUND_TASK_ID = "1"
        runner = lambda argv: self.fail("a throttled status call must not run the delegate script")
        out = self.agent.tool_delegate(action="status", chat_id=1, agent="cal", request_id="req-1", _runner=runner)
        self.assertEqual(out, IN_FLIGHT_REFUSAL)
        self.assertEqual(self.asked, ["req-1"])
        self.agent.BACKGROUND_TASK_ID = ""
        self.agent._background.status_refusal = lambda *a: self.fail("the foreground is never throttled")
        calls = []
        self.agent.tool_delegate(action="status", chat_id=1, agent="cal", request_id="req-1",
                                 _runner=lambda argv: calls.append(argv) or type("R", (), {"returncode": 0, "stdout": "{}", "stderr": ""})())
        self.assertTrue(calls)


if __name__ == "__main__":
    unittest.main(verbosity=2)
