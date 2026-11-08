"""Release safety at the real supervisor, source and process boundaries."""
import os, sys, tempfile, unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch
from tabularium.ops import agent_control
from tabularium.ops import supervisor
from tabularium.ops import release
from tabularium.core.roster import AGENTS
from tabularium.records.worker_lease import WorkerLease
from tabularium.core.agent_runtime import atomic_json

class ReleaseFlow(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.target={'commit':'tested','digest':'abc'}
        self.children=[supervisor.Child(n) for n in AGENTS+('dashboard',)]
        for name,value in [('ROOT',self.root),('STATE',self.root/'logs/supervisor_state.json')]:
            p=patch.object(supervisor,name,value);p.start();self.addCleanup(p.stop)
        p=patch.object(release,'identity',return_value=self.target,create=True);p.start();self.addCleanup(p.stop)
        p=patch.object(release,'uncommitted',return_value=[]);p.start();self.addCleanup(p.stop)
        p=patch.object(release,'LOADED',self.target,create=True);p.start();self.addCleanup(p.stop)
    def request(self):
        return agent_control.request_restart(root=self.root)
    def record(self):
        return supervisor.read_json(self.root/'dashboard/restart_status.json')
    def test_request_includes_dashboard_and_writes_no_direct_drains(self):
        p=patch('subprocess.run',return_value=type('Result',(),{'returncode':0,'stdout':'','stderr':''})());p.start()
        try:self.request()
        finally:p.stop()
        r=self.record()
        self.assertIn('dashboard',r.get('components',[]))
        self.assertEqual(r['target'],self.target)
        self.assertEqual(r['status'],'queued')
        self.assertFalse(any(self.root.glob('*/task_state/drain')))
        self.request();self.assertEqual(self.record()['id'],r['id'])
    def test_failed_suite_never_drains_or_starts_components(self):
        self.request()
        future=Future();future.set_exception(RuntimeError('suite failed'))
        job=supervisor.release_step(self.children,None,lambda *a:future)
        supervisor.release_step(self.children,job,lambda *a:future)
        self.assertEqual(self.record()['status'],'failed')
        self.assertFalse(any(self.root.glob('*/task_state/drain')))
        self.assertFalse(supervisor.release_allows_start('cato',self.record(),self.root))
    def stage_tested(self):
        self.request();r=self.record();r['status']='draining';r['tested']=self.target
        r['previous']={n:'old-'+n for n in r['components']}
        atomic_json(self.root/'dashboard/restart_status.json',r)
        return r
    def test_busy_worker_lease_prevents_replacement_even_after_parent_exits(self):
        r=self.stage_tested();lock=WorkerLease(self.root/'cato/task_state/background/1/worker.lock')
        self.assertTrue(lock.acquire())
        try:
            self.assertFalse(supervisor.release_allows_start('cato',r,self.root))
        finally:lock.close()
        self.assertTrue(supervisor.release_allows_start('cato',r,self.root))
    def test_a_completed_release_on_an_older_commit_never_blocks_a_cold_start(self):
        # 2026-10-29 14:36: the supervisor came back on df40fc0 with the last release complete on 64c3f7d and started nothing.
        r=self.stage_tested();r['status']='complete';r['tested']={'commit':'older','digest':'older'}
        self.assertTrue(supervisor.release_allows_start('cato',r,self.root))
    def test_source_change_after_testing_blocks_every_start(self):
        r=self.stage_tested();self.target['digest']='changed'
        # The persisted tested identity remains abc.
        r=self.record()
        self.assertFalse(supervisor.release_allows_start('dashboard',r,self.root))
        supervisor.release_step(self.children,None,lambda *a:None)
        self.assertEqual(self.record()['status'],'failed')
    def test_partial_release_wrong_version_or_stale_health_never_completes(self):
        r=self.stage_tested()
        view={n:dict(running=True,health_fresh=True,polling_fresh=True,release=self.target,instance='new-'+n) for n in r['components']}
        for field,value in [('release',{}),('health_fresh',False),('running',False),('instance','old-dashboard')]:
            bad={n:dict(v) for n,v in view.items()};bad['dashboard'][field]=value
            self.assertFalse(supervisor.release_complete(r,bad,self.target))
        self.assertTrue(supervisor.release_complete(r,view,self.target))
    def test_old_crash_is_not_a_current_release_failure_after_replacement(self):
        self.stage_tested()
        child=self.children[-1];child.instance='new-dashboard';child.started=200
        child.last_exit={'time':100,'code':1,'planned':False}
        p=patch.object(child,'running',return_value=True);p.start()
        try:supervisor.release_step(self.children,None,lambda *a:None)
        finally:p.stop()
        self.assertNotIn('dashboard: start/crash failure',self.record()['message'])

    def test_failed_start_is_reported_without_crashing_supervisor(self):
        child=self.children[0]
        p=patch.object(supervisor,'start',side_effect=OSError('synthetic start failure'));p.start()
        try:supervisor.start_checked(child)
        finally:p.stop()
        self.assertIsNotNone(child.last_exit)
        self.assertFalse(child.running())
        self.assertEqual(len(child.exits),1)

class TestedSource(unittest.TestCase):
    def test_edit_during_tests_invalidates_test_receipt(self):
        target={'commit':'one','digest':'one'}
        p=patch.object(release,'identity',side_effect=[target,dict(target,digest='two')]);p.start()
        q=patch.object(release,'uncommitted',return_value=[]);q.start()
        cmd=patch('subprocess.run',return_value=type('Result',(),{'returncode':0,'stdout':'','stderr':''})());cmd.start()
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        try:self.assertRaises(RuntimeError,release.test_version,Path(temp.name),target)
        finally:cmd.stop();q.stop();p.stop()


    def test_windowless_supervisor_uses_console_python_for_suites(self):
        target={'commit':'one','digest':'one'}
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        calls=[]
        def run(argv,**kwargs):
            calls.append(argv)
            return type('Result',(),{'returncode':0,'stdout':'','stderr':''})()
        patches=[patch.object(release,'identity',return_value=target),
                 patch.object(release,'uncommitted',return_value=[]),
                 patch.object(sys,'executable',str(Path(sys.executable).with_name('pythonw.exe'))),
                 patch('subprocess.run',side_effect=run)]
        for p in patches:p.start()
        try:
            release.test_version(Path(temp.name),target)
            self.assertEqual([Path(c[0]).name for c in calls],['python.exe'])
        finally:
            for p in reversed(patches):p.stop()

    def test_failed_supervisor_handoff_keeps_the_ownership_socket(self):
        import socket
        lock=socket.socket();lock.bind(('127.0.0.1',0));address=lock.getsockname()
        p=patch('subprocess.Popen',side_effect=OSError('cannot launch replacement'));p.start()
        try:
            self.assertFalse(supervisor.handoff_supervisor(lock))
            self.assertEqual(lock.getsockname(),address)
        finally:p.stop();lock.close()

    def test_handoff_returns_the_fleet_to_task_scheduler_and_exits_non_zero(self):
        # 2026-10-29 13:38: the replacement used to be the old supervisor's own child, so every release inherited the
        # shell that started the first one; the Codex app's update killed the whole fleet while the task sat Ready.
        import socket
        lock=socket.socket();lock.bind(('127.0.0.1',0));launched=[];exits=[]
        patches=[patch('subprocess.Popen',side_effect=lambda argv,**k:launched.append(argv)),
                 patch('os._exit',side_effect=lambda code:exits.append(code))]
        for p in patches:p.start()
        try:
            supervisor.handoff_supervisor(lock)
            self.assertEqual(exits,[supervisor.RESTART_EXIT])
            self.assertEqual(Path(launched[0][0]).name,'python.exe')
            self.assertIn("'schtasks', '/Run', '/TN', 'AgentsSupervisor'",launched[0][2])
            self.assertNotIn('supervisor.py',' '.join(launched[0]))
        finally:
            for p in reversed(patches):p.stop()
            lock.close()

    def test_a_supervisor_started_by_hand_defers_to_the_scheduled_task(self):
        def run(argv,**kwargs):
            calls.append(argv)
            return type('R',(),{'returncode':rc,'stdout':status})()
        for rc,status,expect in [(0,'Status: Ready\n',True),(0,'Status: Running\n',False),(1,'',False)]:
            calls=[]
            with patch('subprocess.run',side_effect=run):
                self.assertEqual(supervisor.scheduler_owns_start(),expect,(rc,status))
            self.assertEqual([c[1] for c in calls],['/Query','/Run'] if expect else ['/Query'])


class BusyBoundaries(unittest.TestCase):
    def test_foreground_drain_waits_for_background_ownership(self):
        from tabularium.core.engine import AgentRuntime
        from tabularium.core.agent_runtime import DurableInbox
        from types import SimpleNamespace
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);root=Path(temp.name)
        agent=AgentRuntime();agent._background=SimpleNamespace(processes={},agent_dir=root)
        inbox=DurableInbox(root,lambda *a:None)
        lock=WorkerLease(root/'task_state/background/1/worker.lock');self.assertTrue(lock.acquire())
        try:self.assertFalse(agent.wait_for_quiet(inbox,timeout=0.05,poll=0.01))
        finally:lock.close()
        self.assertTrue(agent.wait_for_quiet(inbox,timeout=0.05,poll=0.01))

    def test_dashboard_finishes_inflight_request_before_acknowledging(self):
        import threading, urllib.request
        from http.server import BaseHTTPRequestHandler
        from tabularium.dashboard import server as dashboard
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);root=Path(temp.name)
        entered=threading.Event();finish=threading.Event();closing=threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                entered.set();finish.wait(10)
                self.send_response(200);self.end_headers();self.wfile.write(b'finished')
            def log_message(self,*a):pass
        class Server(dashboard._ExclusiveServer):
            def server_close(self):
                closing.set();super().server_close()
        server=Server(('127.0.0.1',0),Handler);result=[];response=[]
        env=patch.dict(os.environ,AGENT_LAUNCH_ID='dashboard-test');env.start()
        thread=threading.Thread(target=lambda:result.append(dashboard.serve(server,root)))
        def fetch():
            connection=urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}',timeout=10)
            try:response.append(connection.read())
            finally:connection.close()
        client=threading.Thread(target=fetch)
        thread.start();client.start()
        try:
            self.assertTrue(entered.wait(5))
            atomic_json(root/'dashboard/task_state/drain',{'id':'r','instance':'dashboard-test'})
            self.assertTrue(closing.wait(5))
            self.assertTrue(thread.is_alive())
            self.assertNotIn('restart',supervisor.read_json(root/'dashboard/task_state/health.json'))
            finish.set();client.join(5);thread.join(5)
            self.assertEqual(result,[75]);self.assertEqual(response,[b'finished'])
            self.assertEqual(supervisor.read_json(root/'dashboard/task_state/health.json')['restart']['id'],'r')
        finally:
            finish.set();client.join(10);thread.join(10);env.stop()

if __name__=='__main__':unittest.main()
