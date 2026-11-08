import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_control
import supervisor
from agent_core.roster import AGENTS
from agent_runtime import atomic_json


class RestartEvidence(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_status_never_uses_idle_from_a_dead_process(self):
        now = time.time()
        atomic_json(self.root/'logs/supervisor_state.json', {'updated': now, 'children': {
            name: {'running': False, 'pid': None} for name in AGENTS}})
        for name in AGENTS:
            atomic_json(self.root/name/'task_state/health.json', {'pid': 42, 'phase': 'idle', 'last_progress': now})
        result = agent_control.status(self.root)
        self.assertNotIn('idle', result)
        self.assertNotIn('pid=42', result)

    def test_three_planned_exits_restart_immediately_for_every_agent(self):
        for name in AGENTS:
            child = supervisor.Child(name)
            for number in range(3):
                child.instance = str(number)
                child.python_pid = 42
                child.proc = subprocess.Popen([sys.executable, '-c', 'raise SystemExit(75)'],
                                              creationflags=subprocess.CREATE_NO_WINDOW)
                child.proc.wait(timeout=10)
                request = {'id': str(number), 'instance': child.instance}
                health = {'instance': child.instance, 'pid': 42, 'restart': request, 'last_progress': 1000, 'phase': 'restart_ready'}
                supervisor.observe_exit(child, 1000+number, health, request)
                self.assertEqual(supervisor.decide(child, 1000+number), 'start')
            self.assertEqual(child.exits, [])

    def test_crashes_and_wrong_acknowledgments_consume_crash_budget(self):
        for code, acknowledged in [(1, True), (0, True), (75, False)]:
            child = supervisor.Child('cato')
            child.instance = 'current'
            child.python_pid = 42
            for number in range(3):
                child.proc = subprocess.Popen([sys.executable, '-c', f'raise SystemExit({code})'],
                                              creationflags=subprocess.CREATE_NO_WINDOW)
                child.proc.wait(timeout=10)
                child.instance = 'current'
                child.python_pid = 42
                request = {'id': 'request', 'instance': 'current'}
                health = {'pid': 42, 'instance': 'current', 'last_progress': 1000, 'phase': 'restart_ready',
                          'restart': request if acknowledged else {'id': 'old', 'instance': 'old'}}
                supervisor.observe_exit(child, 1000+number, health, request)
            self.assertEqual(supervisor.decide(child, 1003), 'wait')
            self.assertEqual(supervisor.decide(child, 1303), 'start')

class LiveEvidence(unittest.TestCase):
    def setUp(self):
        import os
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.now = time.time()
        self.pid = os.getpid()
        self.created = supervisor.process_created(self.pid)
        self.record = {'pid': self.pid, 'created': self.created, 'instance': 'current', 'python_pid': self.pid,
                       'started': self.created, 'running': True}
        self.state = {'pid': self.pid, 'created': self.created, 'updated': self.now,
                      'children': {name: dict(self.record) for name in AGENTS}}
        for name in AGENTS:
            atomic_json(self.root/name/'task_state/health.json', {'pid': self.pid, 'instance': 'current',
                        'phase': 'idle', 'last_progress': self.now})
            atomic_json(self.root/name/'task_state/transport.json', {'pid': self.pid, 'poll_ok': True, 'time': self.now})
        self.save()

    def save(self):
        atomic_json(self.root/'logs/supervisor_state.json', self.state)

    def view(self):
        return supervisor.live_status(self.root, self.now)['agents']

    def test_a_health_file_written_after_the_status_clock_was_read_is_fresh_not_stale(self):
        # The agents write health every second; a status call samples its clock first, so a file a few
        # milliseconds newer than that clock is the normal case, not evidence of anything (2026-10-27).
        for name in AGENTS:
            atomic_json(self.root/name/'task_state/health.json', {'pid': self.pid, 'instance': 'current',
                        'phase': 'idle', 'last_progress': self.now+0.05})
            atomic_json(self.root/name/'task_state/transport.json', {'pid': self.pid, 'poll_ok': True, 'time': self.now+0.05})
        self.state['updated'] = self.now+0.05
        self.save()
        self.assertTrue(all(a['status'] == 'ready' for n,a in self.view().items() if n in AGENTS))

    def test_ready_requires_fresh_owned_health_and_live_supervisor(self):
        self.assertTrue(all(a['status'] == 'ready' for n,a in self.view().items() if n in AGENTS))
        for name in AGENTS:
            for bad in [{'instance': 'old'}, {'last_progress': self.now-151}, {'pid': 99999999}]:
                health = {'pid': self.pid, 'instance': 'current', 'phase': 'idle', 'last_progress': self.now}
                atomic_json(self.root/name/'task_state/health.json', dict(health, **bad))
                self.assertNotEqual(self.view()[name]['status'], 'ready')
                self.assertEqual(self.view()[name]['phase'], 'unknown')
            atomic_json(self.root/name/'task_state/health.json', health)
        self.state['updated'] = self.now-31
        self.save()
        self.assertEqual(self.view()['cato']['status'], 'unknown')
        self.state['updated'] = self.now
        self.state['created'] -= 1  # PID reused by a different supervisor.
        self.save()
        self.assertEqual(self.view()['cato']['status'], 'unknown')

    def test_denied_process_query_is_unknown_not_a_status_crash(self):
        import pywintypes
        query = patch('win32api.OpenProcess', side_effect=pywintypes.error(5, 'OpenProcess', 'Access is denied.'))
        query.start()
        try:
            self.assertEqual(self.view()['cato']['status'], 'unknown')
        finally:
            query.stop()

    def test_denied_child_query_does_not_claim_stopped(self):
        self.state['children']['cato']['pid'] = 99
        self.save()
        query = patch.object(supervisor, 'process_created', side_effect=lambda pid: self.created if pid == self.pid else None)
        query.start()
        try:
            self.assertEqual(self.view()['cato']['status'], 'unknown')
        finally:
            query.stop()

    def test_dead_child_and_pid_reuse_override_recent_healthy_snapshot(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            self.state['children']['cato'].update(pid=child.pid, created=supervisor.process_created(child.pid))
            self.save()
            child.kill(); child.wait(timeout=10)
            self.assertEqual(self.view()['cato']['status'], 'stopped')
        finally:
            if child.poll() is None:
                child.kill(); child.wait(timeout=10)
        self.state['children']['cato'] = dict(self.record, created=self.created-1)
        self.save()
        self.assertEqual(self.view()['cato']['status'], 'stopped')

    def test_supervisor_owns_durable_targeted_drains(self):
        from concurrent.futures import Future
        from agent_core import release
        target={'commit':'test','digest':'test'}
        p=patch.object(release,'identity',return_value=target);p.start()
        q=patch.object(release,'uncommitted',return_value=[]);q.start()
        r=patch.object(supervisor,'ROOT',self.root);r.start()
        try:
            agent_control.request_restart(root=self.root)
            children=[]
            for name in AGENTS:
                child=supervisor.Child(name);child.adopted=self.pid;child.instance='current';children.append(child)
            future=Future();future.set_result(target)
            supervisor.release_step(children,None,lambda *a:future)
            first=supervisor.read_json(self.root/'cato/task_state/drain')
            agent_control.request_restart(['cato'],root=self.root)
            self.assertEqual(first,supervisor.read_json(self.root/'cato/task_state/drain'))
            self.assertEqual(first['instance'],'current')
            self.assertEqual(self.view()['cato']['status'],'draining')
            atomic_json(self.root/'cato/task_state/drain',dict(first,instance='old'))
            self.assertEqual(self.view()['cato']['status'],'ready')
        finally:r.stop();q.stop();p.stop()

    def test_adoption_retains_exit_code_identity_and_crash_budget(self):
        import os
        marker = self.root/'exit'
        child = subprocess.Popen([sys.executable, '-c',
            "import sys,time; from pathlib import Path; p=Path(sys.argv[1]); "
            "exec('while not p.exists(): time.sleep(0.02)'); raise SystemExit(75)", str(marker)],
            creationflags=subprocess.CREATE_NO_WINDOW)
        adopted = supervisor.Child('cato')
        try:
            created = supervisor.process_created(child.pid)
            self.state['children']['cato'] = dict(self.record, pid=child.pid, created=created, crashes=[self.now-1])
            self.save()
            row = {'ProcessId': child.pid, 'ParentProcessId': os.getpid(), 'Name': 'python.exe',
                   'CommandLine': 'D:\\Agents\\cato\\telegram.py'}
            query = patch('subprocess.run', return_value=type('Result', (), {'stdout': json.dumps([row])})())
            state = patch.object(supervisor, 'STATE', self.root/'logs/supervisor_state.json')
            query.start(); state.start()
            try:
                supervisor.adopt_existing([adopted])
            finally:
                state.stop(); query.stop()
            self.assertEqual(adopted.instance, 'current')
            self.assertEqual(adopted.exits, [self.now-1])
            marker.touch()
            child.wait(timeout=10)
            self.assertFalse(adopted.running())
            self.assertEqual(adopted.mark_exit(), 75)
        finally:
            if child.poll() is None:
                child.kill(); child.wait(timeout=10)
            if adopted.handle is not None:
                adopted.handle.Close()


class DrainFlow(unittest.TestCase):
    def test_real_runtime_drains_and_acknowledges_for_all_agents(self):
        code = '''
import os, sys, threading
from pathlib import Path
from agent_runtime import DurableInbox
from engine import AgentRuntime
root = Path(sys.argv[1])
a = AgentRuntime()
a.AGENT_DIR = root
a._inbox = DurableInbox(root, lambda *args: None)
a._inbox.progress('idle')
class Bot:
    def __init__(self): self.stopped = threading.Event()
    def infinity_polling(self, **kwargs): self.stopped.wait(15)
    def stop_polling(self): self.stopped.set()
a.bot = Bot()
a.serve_forever()
'''
        import os
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        for name in AGENTS:
            folder = root/name
            folder.mkdir()
            instance = name+'-instance'
            env = dict(os.environ, AGENT_LAUNCH_ID=instance, AGENT_SELFTEST='0')
            proc = subprocess.Popen([sys.executable, '-B', '-c', code, str(folder)], env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                deadline = time.monotonic()+20
                while not (folder/'task_state/health.json').exists() and proc.poll() is None and time.monotonic()<deadline:
                    time.sleep(0.02)
                health = supervisor.read_json(folder/'task_state/health.json')
                self.assertEqual(health.get('instance'), instance)
                atomic_json(folder/'task_state/drain', {'id': 'fixture', 'instance': instance})
                out, err = proc.communicate(timeout=20)
                self.assertEqual(proc.returncode, 75, (out,err))
                request = supervisor.read_json(folder/'task_state/drain')
                health = supervisor.read_json(folder/'task_state/health.json')
                self.assertEqual(health['restart'], request)
                child = supervisor.Child(name)
                child.proc, child.instance = proc, instance
                # No supervisor tick between starting and exiting: still correctly classified.
                supervisor.observe_exit(child, time.time(), health, request)
                self.assertTrue(child.last_exit['planned'])
                self.assertEqual(child.exits, [])
            finally:
                if proc.poll() is None:
                    proc.kill(); proc.wait(timeout=10)
                proc.stdout.close(); proc.stderr.close()


if __name__ == '__main__':
    unittest.main()
