"""Process lifecycle (2026-10-09): supervisor decisions, drain-flag restarts, quiet detection."""
import os, sys, json, sqlite3, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "cato"))      # engine imports the agent-folder modules (openai_chatgpt, ...)
import supervisor
import agent_control


class Decide(unittest.TestCase):
    def test_start_when_not_running(self):
        c = supervisor.Child("cato")
        self.assertEqual(supervisor.decide(c, 1000.0), "start")

    def test_paused_does_nothing(self):
        c = supervisor.Child("cato")
        self.assertIsNone(supervisor.decide(c, 1000.0, paused=True))

    def test_crash_loop_waits_then_starts_again(self):
        c = supervisor.Child("cato")
        c.exits = [900.0, 950.0, 990.0]
        self.assertEqual(supervisor.decide(c, 1000.0), "wait")
        self.assertEqual(supervisor.decide(c, 990.0 + supervisor.CRASH_BACKOFF + 1), "start")

    def test_stalled_child_is_killed_only_past_deadline_and_only_its_own_health(self):
        class Proc:
            pid = 77
            def poll(self): return None
        c = supervisor.Child("cato"); c.proc = Proc(); c.python_pid = 500; c.instance = "current"
        live = {"phase": "model", "deadline": 2000.0, "pid": 500, "instance": "current", "last_progress": 0}
        self.assertIsNone(supervisor.decide(c, 2100.0, live))                                  # within grace
        self.assertEqual(supervisor.decide(c, 2000.0 + supervisor.STALL_GRACE + 1, live), "kill")
        stale = dict(live, pid=499)                                                             # another process's file
        self.assertIsNone(supervisor.decide(c, 9999.0, stale))
        self.assertIsNone(supervisor.decide(c, 9999.0, dict(live, phase="idle")))


class Restart(unittest.TestCase):
    def test_flags_written_without_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            done = agent_control.request_restart(["cato", "vitruvius"], run_tests=False, root=tmp)
            self.assertEqual(done, ["cato", "vitruvius"])
            self.assertTrue((Path(tmp) / "cato" / "task_state" / "drain").exists())
            self.assertFalse((Path(tmp) / "tacitus" / "task_state" / "drain").exists())

    def test_failed_tests_write_no_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            from types import SimpleNamespace
            with patch.object(agent_control, "subprocess", create=True) as sp:
                pass
            with patch("subprocess.run", return_value=SimpleNamespace(returncode=1, stdout="", stderr="boom")):
                with self.assertRaises(RuntimeError):
                    agent_control.request_restart(["cato"], run_tests=True, root=tmp)
            self.assertFalse((Path(tmp) / "cato" / "task_state" / "drain").exists())

    def test_unknown_agent_refused(self):
        with self.assertRaises(ValueError):
            agent_control.request_restart(["nobody"], run_tests=False)


class Quiet(unittest.TestCase):
    def test_wait_for_quiet_reads_the_inbox(self):
        import engine
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "inbox.sqlite3"
            con = sqlite3.connect(path)
            con.execute("CREATE TABLE jobs(id INTEGER PRIMARY KEY, status TEXT)")
            con.execute("INSERT INTO jobs(status) VALUES ('running')"); con.commit(); con.close()
            from contextlib import contextmanager
            class Inbox:
                @contextmanager
                def db(self):
                    c = sqlite3.connect(path)
                    try:
                        yield c
                    finally:
                        c.close()
            self.assertFalse(engine.AgentRuntime().wait_for_quiet(Inbox(), timeout=0.3, poll=0.1))
            c = sqlite3.connect(path); c.execute("UPDATE jobs SET status='done'"); c.commit(); c.close()
            self.assertTrue(engine.AgentRuntime().wait_for_quiet(Inbox(), timeout=1, poll=0.1))



class Liveness(unittest.TestCase):
    def test_silent_health_file_means_kill(self):
        class Proc:
            pid = 77
            def poll(self): return None
        c = supervisor.Child("cato"); c.proc = Proc(); c.python_pid = 500; c.instance = "current"
        quiet = {"phase": "idle", "pid": 500, "instance": "current", "last_progress": 1000.0}
        self.assertIsNone(supervisor.decide(c, 1000.0 + supervisor.HEALTH_STALE - 1, quiet))
        self.assertEqual(supervisor.decide(c, 1000.0 + supervisor.HEALTH_STALE + 1, quiet), "kill")
        self.assertIsNone(supervisor.decide(c, 9999.0, dict(quiet, pid=499)))        # someone else's health file

    def test_adopted_child_is_tracked_until_it_exits(self):
        import subprocess, sys as _sys
        p = subprocess.Popen([_sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            c = supervisor.Child("vitruvius"); c.adopted = p.pid
            self.assertTrue(c.tracked() and c.running())
            self.assertEqual(c.pid(), p.pid)
        finally:
            p.kill(); p.wait()
        time.sleep(0.2)
        self.assertFalse(c.running())
        self.assertEqual(c.mark_exit(), "unknown (adopted)")
        self.assertFalse(c.tracked())

if __name__ == "__main__":
    unittest.main()
