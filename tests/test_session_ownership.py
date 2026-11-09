import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_harness import make_agent, put
from tabularium.core.session_lifecycle import SessionLimit, continue_turn
from tabularium.records.worker_lease import WorkerLease
from tabularium.core.roster import ROLES
from tabularium.core.turn_loop import run, Exchange
from tabularium.records.background_runtime import TaskStore, worker
from tabularium.models import subscription_runtime as bridge
import claude_agent_sdk as sdk
from claude_agent_sdk._errors import ResultError


class SessionLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_sdk_limit_forms_continue_on_all_agents_without_replaying_tool(self):
        for name in ROLES:
            for throws in (False, True):
                (self.root / (name + str(throws))).mkdir()
                agent = make_agent(name, self.root / (name + str(throws)))
                agent._background_boundary = lambda *a: []
                agent._load_core_memory = lambda: ''
                file = agent.AGENT_DIR / 'evidence.txt'
                file.write_text('completed evidence unique 73',encoding='utf-8')
                messages = [{'role':'user','content':'Read evidence then answer'}]
                agent.conversations[1] = messages
                captured = {'sessions':0,'calls':0,'closed':0}
                def server(n,v,tools):
                    captured['tools'] = {t.name:t for t in tools}
                    return {}
                async def query(prompt,options):
                    captured['sessions'] += 1
                    incoming = [m async for m in prompt]
                    try:
                        if captured['sessions'] == 1:
                            captured['calls'] += 1
                            await captured['tools']['read_file'].handler({'path':str(file)})
                            if throws:
                                raise ResultError('limit',data={'subtype':'error_max_turns','terminal_reason':'max_turns'})
                            yield sdk.ResultMessage(subtype='error_max_turns',duration_ms=1,duration_api_ms=1,is_error=True,num_turns=1,session_id='one')
                        else:
                            self.assertEqual(captured['closed'],1)
                            self.assertIn('completed evidence unique 73',json.dumps(incoming))
                            yield sdk.ResultMessage(subtype='success',duration_ms=1,duration_api_ms=1,is_error=False,num_turns=1,session_id='two',structured_output={'reply':'Done'})
                    finally:
                        captured['closed'] += 1
                patches = [patch.object(bridge,'find_cli',return_value='fake.exe'),
                           patch.object(bridge,'subscription_ready',return_value=(True,'')),
                           patch.object(sdk,'create_sdk_mcp_server',side_effect=server),
                           patch.object(sdk,'query',side_effect=query)]
                for p in patches: p.start()
                try:
                    answer = continue_turn(agent,messages,1,lambda: bridge.run_claude(agent,messages,1,'test'))
                finally:
                    for p in reversed(patches): p.stop()
                self.assertEqual(answer,'Done')
                self.assertEqual(captured['sessions'],2)
                self.assertEqual(captured['calls'],1)
                self.assertTrue((agent.RUNTIME_DIR/'task_state/session_checkpoint.json').exists())

    def test_api_limit_preserves_receipt_then_finishes(self):
        agent = make_agent('coordinator',self.root)
        agent.MAX_TOOL_ITERATIONS = 1
        agent._background_boundary = lambda *a: []
        agent.execute_tool = lambda *a: 'one durable result'
        messages = [{'role':'user','content':'work'}]
        agent.conversations[1] = messages
        class Provider:
            calls = 0
            def exchange(self, history):
                self.calls += 1
                if self.calls == 1:
                    return Exchange(blocks=[{'type':'tool_use','id':'one','name':'read_file','input':{}}],calls=[('one','read_file',{})])
                return Exchange(text='{"reply":"Done"}')
        provider = Provider()
        self.assertEqual(continue_turn(agent,messages,1,lambda: run(agent,messages,1,provider)),'Done')
        self.assertEqual(provider.calls,2)

    def test_unknown_action_and_zero_progress_never_auto_continue(self):
        agent = make_agent('coordinator',self.root)
        for pending in (False,True):
            messages = [{'role':'user','content':'work'}]
            agent.conversations[1] = messages
            count = []
            def attempt():
                count.append(1)
                if pending:
                    folder = agent.RUNTIME_DIR/'task_state/actions'
                    folder.mkdir(parents=True,exist_ok=True)
                    (folder/'unknown.json').write_text(json.dumps({'status':'started','tool':'send_email'}))
                raise SessionLimit('test')
            try:
                continue_turn(agent,messages,1,attempt)
                self.fail('must stop')
            except RuntimeError as exc:
                self.assertIn('unknown action' if pending else 'without completed tool progress',str(exc))
            self.assertEqual(len(count),1)


class Ownership(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_live_lease_blocks_resume_and_other_tasks_even_after_manager_restart(self):
        store = TaskStore(self.root)
        first = store.start(1,'first')['id']
        row = store.claim_next()
        lease = WorkerLease(store.directory(first)/'worker.lock')
        self.assertTrue(lease.acquire())
        try:
            put(store, first,'interrupted','Worker error: simulated')
            store.control(1,first,'resume')
            store.start(1,'second')
            restarted = TaskStore(self.root)
            self.assertIsNone(restarted.claim_next())
            self.assertTrue(restarted.lease_busy(first))
        finally:
            lease.close()
        replacement = restarted.claim_next()
        self.assertEqual(replacement['id'],first)
        self.assertNotEqual(replacement['attempt'],row['attempt'])
        store.worker_attempt = row['attempt']
        try:
            store.move(first,'finish','finished','stale')
            self.fail('stale writer accepted')
        except RuntimeError:
            pass
        worker(first,self.root,ROLES['coordinator'],row['attempt'])
        self.assertEqual(restarted.get(first)['status'],'starting')
        self.assertEqual(restarted.get(first)['attempt'],replacement['attempt'])

    def test_real_process_lease_is_recoverable_after_exit(self):
        store = TaskStore(self.root)
        ident = store.start(1,'work')['id']
        folder = store.directory(ident)
        code = "from pathlib import Path; import time; from tabularium.records.worker_lease import WorkerLease; p=Path(__import__('sys').argv[1]); l=WorkerLease(p/'worker.lock'); assert l.acquire(); (p/'ready').touch(); time.sleep(30)"
        proc = subprocess.Popen([sys.executable,'-B','-c',code,str(folder)],creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            deadline = time.monotonic()+5
            while not (folder/'ready').exists() and time.monotonic()<deadline:
                time.sleep(.02)
            self.assertTrue((folder/'ready').exists())
            self.assertIsNone(TaskStore(self.root).claim_next())
        finally:
            proc.terminate()
            proc.wait(timeout=5)
        self.assertFalse(store.lease_busy(ident))
        self.assertEqual(store.claim_next()['id'],ident)

    def test_two_managers_cannot_reserve_the_same_slot(self):
        import concurrent.futures
        store = TaskStore(self.root)
        store.start(1,'first')
        store.start(1,'second')
        other = TaskStore(self.root)
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        try:
            results = list(pool.map(lambda s:s.claim_next(),[store,other]))
        finally:
            pool.shutdown()
        self.assertEqual(sum(r is not None for r in results),1)

    def test_retry_does_not_requeue_worker_still_holding_lease(self):
        store = TaskStore(self.root)
        ident = store.start(1,'work')['id']
        put(store, ident,'interrupted','Worker error: test')
        db = sqlite3.connect(store.database)
        try:
            db.execute('UPDATE tasks SET updated=0 WHERE id=?',(ident,))
            db.commit()
        finally:
            db.close()
        lease = WorkerLease(store.directory(ident)/'worker.lock')
        self.assertTrue(lease.acquire())
        try:
            store.retry_interrupted()
            self.assertEqual(store.get(ident)['status'],'interrupted')
        finally:
            lease.close()
        store.retry_interrupted()
        self.assertEqual(store.get(ident)['status'],'queued')

    def test_real_worker_continues_in_same_process_and_releases_ownership(self):
        from test_worker import STUB
        # Exercise the actual subprocess entry point and full worker cleanup, without a live model.
        stub = STUB[:STUB.index('def run_turn(')] + '''
from tabularium.core.session_lifecycle import continue_turn, SessionLimit
from tabularium.core.agent_runtime import atomic_json
_inbox.record_event = lambda *a, **kw: None
rounds = 0
def run_turn(messages, chat):
    def session():
        global rounds
        rounds += 1
        if rounds == 1:
            messages.extend([{"role":"assistant","content":[{"type":"tool_use","id":"once","name":"test","input":{}}]},
                             {"role":"user","content":[{"type":"tool_result","tool_use_id":"once","content":"saved"}]}])
            atomic_json(RUNTIME_DIR / "first_pid.json", {"pid":os.getpid()})
            raise SessionLimit("fixture session")
        atomic_json(RUNTIME_DIR / "second_pid.json", {"pid":os.getpid()})
        raise RunawayStop("fixture ends after proving continuation")
    import sys
    return continue_turn(sys.modules[__name__] if __name__ in sys.modules else NS(RUNTIME_DIR=RUNTIME_DIR, save_conversation=save_conversation, _inbox=_inbox), messages, chat, session)
'''
        (self.root/'agent.py').write_text(stub,encoding='utf-8')
        store = TaskStore(self.root)
        ident = store.start(1,'work')['id']
        store.launch_next()
        proc = store.processes[ident]
        proc.wait(timeout=10)
        folder = store.directory(ident)
        self.assertEqual(proc.returncode,0,(folder/'worker.log').read_text(encoding='utf-8'))
        self.assertEqual(json.loads((folder/'first_pid.json').read_text()),json.loads((folder/'second_pid.json').read_text()))
        self.assertEqual(store.get(ident)['pid'],proc.pid)
        self.assertFalse(store.lease_busy(ident))
        self.assertTrue((folder/'task_state/session_checkpoint.json').exists())

    def test_drain_prevents_claim_and_spawn(self):
        store = TaskStore(self.root)
        ident = store.start(1,'work')['id']
        drain = self.root/'task_state/drain'
        drain.touch()
        self.assertIsNone(store.claim_next())
        drain.unlink()
        reserved = store.claim_next()
        drain.touch()
        store.launch(reserved)
        self.assertEqual(store.processes,{})
        self.assertEqual(store.get(ident)['status'],'queued')

    def test_legacy_launch_receives_an_identity_under_lease(self):
        from test_worker import STUB
        (self.root/'agent.py').write_text(STUB,encoding='utf-8')
        store = TaskStore(self.root)
        ident = store.start(1,'work')['id']
        put(store, ident,'starting')
        env = dict(os.environ,COORDINATOR_BACKGROUND_TASK=str(ident))
        proc = subprocess.run([sys.executable,'-B','-m','tabularium','worker',str(ident),
                               '--agent',str(self.root),'--role','coordinator'],env=env,capture_output=True,timeout=10,
                              creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertEqual(proc.returncode,0,proc.stderr)
        self.assertTrue(store.get(ident)['attempt'])
        self.assertFalse(store.lease_busy(ident))


if __name__ == '__main__':
    unittest.main()
