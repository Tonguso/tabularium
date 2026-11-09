import json
import tempfile
import unittest
from pathlib import Path

from tests.agent_harness import make_agent, put
from tabularium.core.roster import ROLES
from tabularium.core.turn_loop import run, Exchange
from tabularium.core.session_lifecycle import continue_turn
from tabularium.records.task_decisions import answer_record
from tabularium.records.worker_lease import WorkerLease


class WaitingHandoff(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_question_mid_batch_preserves_completed_result_and_stops_next_action(self):
        for name in ROLES:
            folder = self.root/name
            folder.mkdir()
            agent = make_agent(name, folder, background=True)
            store = agent._background
            ident = 1
            agent.BACKGROUND_TASK_ID = str(ident)
            put(store, ident, 'running')
            messages = [{'role':'user','content':'Do the work'}]
            agent.conversations[1] = messages
            calls = []
            decision = []
            def execute(tool, inputs, chat):
                calls.append(tool)
                decision.append(store.ask(1, ident, 'Choose A or B'))
                return 'First action completed; durable output 73'
            agent.execute_tool = execute
            class Provider:
                def exchange(self, history):
                    if calls:
                        return Exchange(text='{"reply":"incorrectly continued"}')
                    blocks = [{'type':'tool_use','id':i,'name':i,'input':{}} for i in ('first','second')]
                    return Exchange(blocks=blocks, calls=[(i,i,{}) for i in ('first','second')])
            lease = WorkerLease(store.directory(ident)/'worker.lock')
            self.assertTrue(lease.acquire())
            try:
                result = continue_turn(agent, messages, 1, lambda: run(agent,messages,1,Provider()))
                self.assertEqual(calls, ['first'])
                self.assertFalse(result.complete)
                self.assertIn('durable output 73', json.dumps(messages))
                self.assertEqual(store.finish(ident,result), 'needs_answer')
            finally:
                lease.close()
            other = store.start(1,'Unrelated task')['id']
            self.assertEqual(store.claim_next()['id'],other)
            put(store,other,'finished')
            answer_record(store,1,decision[0]['id'],'B','trusted test source')
            answer_record(store,1,decision[0]['id'],'B','trusted test source')
            self.assertEqual(len(store.pending_controls(ident)),1)
            self.assertEqual(store.claim_next()['id'],ident)
            put(store,ident,'running')
            class Resume:
                def exchange(self, history):
                    self_test.assertIn('durable output 73',json.dumps(history))
                    self_test.assertIn('Answer: B',json.dumps(history))
                    return Exchange(text='{"reply":"continued safely"}')
            self_test = self
            resumed = continue_turn(agent,messages,1,lambda: run(agent,messages,1,Resume()))
            self.assertEqual(resumed,'continued safely')
            self.assertEqual(calls,['first'])
            self.assertEqual(store.pending_controls(ident),[])

    def test_answer_during_handoff_queues_once_without_overlapping_owner(self):
        import os, sqlite3, time
        from tabularium.core.agent_runtime import atomic_json
        agent = make_agent('coordinator', self.root, background=True)
        store = agent._background
        messages = [{'role':'user','content':'Do the work'}]
        agent.conversations[1] = messages
        lease = WorkerLease(store.directory(1)/'worker.lock')
        self.assertTrue(lease.acquire())
        try:
            db = sqlite3.connect(store.database)
            try:
                db.execute('UPDATE tasks SET pid=? WHERE id=1',(os.getpid(),))
                db.commit()
            finally:
                db.close()
            atomic_json(store.directory(1)/'task_state/health.json',{'pid':os.getpid(),'deadline':time.time()+30})
            decision = store.ask(1,1,'Choose A or B')
            live = store.live(1,1)
            self.assertEqual(live['state'],'running')
            self.assertTrue(live['pause_pending'])
            result = continue_turn(agent,messages,1,lambda: agent._background_boundary(messages,[],1))
            answer_record(store,1,decision['id'],'B','trusted test source')
            self.assertEqual(store.finish(1,result),'queued')
            self.assertIsNone(store.claim_next())
        finally:
            lease.close()
        self.assertEqual(store.claim_next()['id'],1)
        self.assertEqual(len(store.pending_controls(1)),1)


    def test_late_instruction_is_not_acknowledged_as_seen_by_an_old_model_reply(self):
        agent = make_agent('coordinator',self.root,background=True)
        store = agent._background
        messages = [{'role':'user','content':'Original requirement'}]
        agent.conversations[1] = messages
        class Provider:
            def exchange(self, history):
                store.control(1,1,'steer','Also deliver the second requirement')
                return Exchange(text='{"reply":"Original work only"}')
        run(agent,messages,1,Provider())
        self.assertEqual(len(store.pending_controls(1)),1)



class SubscriptionWaiting(unittest.TestCase):
    setUp = WaitingHandoff.setUp

    def test_sdk_closes_live_session_after_saved_tool_and_resumes_without_replay(self):
        import asyncio
        from unittest.mock import patch
        from tabularium.models import subscription_runtime as bridge
        import claude_agent_sdk as sdk
        for name in ROLES:
            folder = self.root/name
            folder.mkdir()
            agent = make_agent(name,folder,background=True)
            agent._load_core_memory = lambda: ''
            store = agent._background
            messages = [{'role':'user','content':'Read once then ask'}]
            agent.conversations[1] = messages
            file = folder/'evidence.txt'
            file.write_text('already completed evidence 73')
            state = {'closed':0,'calls':0,'sessions':0}
            original = agent.execute_tool
            def execute(tool, inputs, chat):
                result = original(tool,inputs,chat)
                state['calls'] += 1
                state['decision'] = store.ask(1,1,'Choose A or B')
                return result
            agent.execute_tool = execute
            def server(n,v,tools):
                state['tools'] = {t.name:t for t in tools}
                return {}
            async def query(prompt, options):
                state['sessions'] += 1
                incoming = [p async for p in prompt]
                try:
                    if state['sessions'] == 1:
                        await state['tools']['read_file'].handler({'path':str(file)})
                        await asyncio.Event().wait()  # No final SDK response: handoff must close it.
                    else:
                        self.assertIn('already completed evidence 73', json.dumps(incoming))
                        self.assertIn('Answer: B', json.dumps(incoming))
                        yield sdk.ResultMessage(subtype='success',duration_ms=1,duration_api_ms=1,is_error=False,
                                                num_turns=1,session_id='resumed',structured_output={'reply':'continued'})
                finally:
                    state['closed'] += 1
            patches = [patch.object(bridge,'find_cli',return_value='fake.exe'),
                       patch.object(bridge,'subscription_ready',return_value=(True,'')),
                       patch.object(sdk,'create_sdk_mcp_server',side_effect=server),
                       patch.object(sdk,'query',side_effect=query)]
            for p in patches: p.start()
            try:
                result = continue_turn(agent,messages,1,lambda: bridge.run_claude(agent,messages,1,'test'))
                self.assertFalse(result.complete)
                self.assertEqual(state['closed'],1)
                self.assertIn('already completed evidence 73',json.dumps(messages))
                store.finish(1,result)
                answer_record(store,1,state['decision']['id'],'B','trusted test source')
                self.assertEqual(continue_turn(agent,messages,1,lambda: bridge.run_claude(agent,messages,1,'test')),'continued')
                self.assertEqual(state['calls'],1)
                self.assertEqual(state['closed'],2)
            finally:
                for p in reversed(patches): p.stop()


class ProcessHandoff(unittest.TestCase):
    setUp = WaitingHandoff.setUp

    def test_real_worker_releases_slot_runs_next_job_and_resumes_saved_actions(self):
        from test_worker import STUB
        from tabularium.records.background_runtime import TaskStore
        stub = STUB[:STUB.index('def run_turn(')] + '''
from tabularium.core.engine import AgentRuntime
from tabularium.core.session_lifecycle import continue_turn
from tabularium.core.reply_envelope import Reply
runtime = AgentRuntime()
runtime.SYSTEM_PROMPT = SYSTEM_PROMPT
runtime.BACKGROUND_TASK_ID = str(ident)
runtime.RUNTIME_DIR = RUNTIME_DIR
runtime.save_conversation = save_conversation
runtime._inbox = _inbox
runtime._inbox.record_event = lambda *a, **kw: None
def load_conversation(chat):
    path = _conversation_path(chat)
    return json.loads(path.read_text()) if path.exists() else []
def run_turn(messages, chat):
    def session():
        runtime._background_boundary(messages, [], chat)
        completed = RUNTIME_DIR/'completed.txt'
        if ident == 1 and not completed.exists():
            completed.write_text('external side effect once')
            messages.extend([{'role':'assistant','content':[{'type':'tool_use','id':'once','name':'fixture','input':{}}]},
                             {'role':'user','content':[{'type':'tool_result','tool_use_id':'once','content':'completed once 73'}]}])
            save_conversation(chat)
            runtime._background.ask(chat,ident,'Choose A or B')
            runtime._background_boundary(messages, [], chat)
        if ident == 1:
            assert 'completed once 73' in json.dumps(messages)
            assert 'Answer: B' in json.dumps(messages)
            (RUNTIME_DIR/'resumed.txt').write_text('resumed without replay')
        return Reply('fixture completed',complete=False)
    return continue_turn(runtime,messages,chat,session)
'''
        (self.root/'agent.py').write_text(stub,encoding='utf-8')
        store = TaskStore(self.root)
        first = store.start(1,'First task')['id']
        second = store.start(1,'Next task')['id']
        self.assertEqual(store.launch_next(),first)
        process = store.processes[first]
        process.wait(timeout=15)
        self.assertEqual(store.get(first)['status'],'needs_answer',(store.directory(first)/'worker.log').read_text())
        self.assertFalse(store.owner_alive(first))
        artifact = store.directory(first)/'completed.txt'
        stamp = artifact.stat().st_mtime_ns
        self.assertEqual(store.launch_next(),second)
        store.processes[second].wait(timeout=15)
        decision = store.decisions(1,first)[0]
        answer_record(store,1,decision['id'],'B','trusted test source')
        answer_record(store,1,decision['id'],'B','trusted test source')
        self.assertEqual(store.launch_next(),first)
        store.processes[first].wait(timeout=15)
        self.assertTrue((store.directory(first)/'resumed.txt').exists(),(store.directory(first)/'worker.log').read_text())
        self.assertEqual(artifact.stat().st_mtime_ns,stamp)
        self.assertFalse(store.owner_alive(first))


if __name__ == '__main__':
    unittest.main()
