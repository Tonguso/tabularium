"""Completion feedback reaches real provider loops before a task ends."""
import json
from types import SimpleNamespace as NS
from unittest.mock import patch, MagicMock
import tempfile
import unittest
from pathlib import Path
from tabularium.core.roster import AGENTS
from agent_harness import make_agent


class CompletionFeedback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_provider_repair(self):
        from tabularium.models import subscription_runtime as bridge
        import claude_agent_sdk as sdk
        for name in AGENTS:
            for route in ('openai', 'anthropic', 'claude'):
                root = self.root / (name + route)
                root.mkdir()
                agent = make_agent(name, root, background=True)
                artifact = root / 'result.txt'
                artifact.write_text('correct', encoding='utf-8')
                agent._background.checkpoint(1, 1, '', '', [], guesses=[], completion_items=[{
                    'item': 'Correct result', 'check': 'Read result',
                    'verification': {'kind': 'contains', 'paths': [str(artifact)], 'text': 'correct'}}])
                messages = [{'role': 'user', 'content': 'Save the correct result and verify it.'}]
                agent.conversations[1] = messages
                requests, captured, patches = [], {}, []
                final = json.dumps(dict(reply='Saved and checked.', task_outcome='completed'))
                def action(wire):
                    requests.append(str(wire))
                    return len(requests) == 2
                def response(client, **kw):
                    repair = action(kw)
                    block = (NS(type='function_call', name='verify_requirement', call_id='proof',
                                arguments=json.dumps({'task_id':1,'item':1})) if repair else
                             NS(type='message', content=[NS(type='output_text', text=final)]))
                    return NS(output=[block],status='completed',incomplete_details=None,usage=None), '', [], []
                def stream(**kw):
                    repair = action(kw)
                    block = (NS(type='tool_use',id='proof',name='verify_requirement',input={'task_id':1,'item':1})
                             if repair else NS(type='text',text=final))
                    context = MagicMock()
                    context.__enter__.return_value.get_final_message.return_value = NS(
                        content=[block],stop_reason='tool_use' if repair else 'end_turn',usage=None)
                    return context
                def server(name, version, tools):
                    captured['tools'] = tools
                    return {}
                async def query(prompt, options):
                    wire = [m async for m in prompt]
                    if action(wire):
                        tool = next(t for t in captured['tools'] if t.name == 'verify_requirement')
                        await tool.handler({'task_id':1,'item':1})
                    yield sdk.ResultMessage(subtype='success',duration_ms=1,duration_api_ms=1,is_error=False,
                                            num_turns=1,session_id='fake',structured_output=json.loads(final))
                if route == 'openai':
                    agent.OPENAI_API_KEY = 'test'
                    agent._responses_call = response
                    token = agent._routing.ACTIVE.set({'kind':'openai_api','model':'gpt-test'})
                    patches.append(patch.object(agent._routing,'run',side_effect=lambda a,m,c:a.run_turn_openai(m,c)))
                elif route == 'anthropic':
                    agent.anthropic_client = NS(messages=NS(stream=stream))
                    patches.append(patch.object(agent._routing,'run',side_effect=lambda a,m,c:a.run_turn_anthropic(m,c)))
                else:
                    patches.extend([patch.object(bridge,'find_cli',return_value='fake.exe'),
                                    patch.object(bridge,'auth_status',return_value={'loggedIn':True,'authMethod':'claude.ai'}),
                                    patch.object(sdk,'create_sdk_mcp_server',side_effect=server),
                                    patch.object(sdk,'query',side_effect=query),
                                    patch.object(agent._routing,'run',side_effect=lambda a,m,c:bridge.run_claude(a,m,c,'claude-test'))])
                for patcher in patches:
                    patcher.start()
                try:
                    result = agent.run_turn(messages, 1)
                finally:
                    for patcher in reversed(patches):
                        patcher.stop()
                    if route == 'openai':
                        agent._routing.ACTIVE.reset(token)
                self.assertEqual(agent._background.finish(1,result), 'finished', (name,route))
                self.assertIn('Check unverified', requests[1])
                self.assertNotIn('Runtime feedback', json.dumps(messages))
                self.assertEqual(artifact.read_text(), 'correct')
                self.assertEqual([m['content'] for m in messages if m['role']=='assistant' and isinstance(m['content'],str)],
                                 ['Saved and checked.'])
    def scripted(self, agent, steps):
        from tabularium.core import turn_loop
        wires = []
        class Provider:
            def exchange(self, messages):
                wires.append(json.loads(json.dumps(messages)))
                if len(wires) > len(steps):
                    raise AssertionError('Completion repair did not stop')
                step = steps[len(wires)-1]
                return step() if callable(step) else turn_loop.Exchange(text=json.dumps(step))
        messages = [{'role':'user','content':'Finish the saved work.'}]
        agent.conversations[1] = messages
        patched = patch.object(agent._routing,'run',side_effect=lambda a,m,c:turn_loop.run(a,m,c,Provider()))
        patched.start()
        try:
            result = agent.run_turn(messages,1)
        finally:
            patched.stop()
        return result, wires, messages

    def prepared(self, suffix='test'):
        from tabularium.records import verification
        root = self.root/suffix
        root.mkdir()
        agent = make_agent('coordinator',root,background=True)
        artifact = root/'result.txt'
        artifact.write_text('correct',encoding='utf-8')
        agent._background.checkpoint(1,1,'','',[],guesses=[],completion_items=[{
            'item':'Result correct','check':'Read saved bytes',
            'verification':{'kind':'contains','paths':[str(artifact)],'text':'correct'}}])
        agent._execute_tool_unguarded('verify_requirement',{'task_id':1,'item':1},1)
        receipt = verification.receipt_path(agent._background,1,'',1)
        return agent, artifact, receipt

    def test_missing_outcome_repairs_without_repeating_valid_proof(self):
        agent, artifact, receipt = self.prepared()
        before = receipt.read_bytes()
        result, wires, messages = self.scripted(agent,[dict(reply='Saved'),dict(reply='Saved',task_outcome='completed')])
        self.assertEqual(agent._background.finish(1,result),'finished')
        self.assertEqual(len(wires),2)
        self.assertEqual(receipt.read_bytes(),before)
        self.assertIn('did not declare a complete result',json.dumps(wires[1]))
        self.assertEqual(len(messages),2)

    def test_ordinary_success_waits_and_explicit_blockers_add_no_model_calls(self):
        from tabularium.core.reply_envelope import Reply
        from tabularium.records.task_decisions import finish
        for mode in ('success','blocked','waiting','question','decision','delegate','trial','cancelled'):
            agent, artifact, receipt = self.prepared(mode)
            store = agent._background
            result = dict(reply='Outcome',task_outcome='completed')
            if mode in ('blocked','waiting'):
                result['task_outcome']=mode
            elif mode=='question':
                result['question']='Which currency?'
            elif mode=='decision':
                store.ask(1,1,'Which currency?')
            elif mode=='delegate':
                store.add_delegation(1,'vit','pending','Check file','worker_judgment')
                store.status_checker=lambda *args:'running'
            elif mode=='trial':
                (store.directory(1)/'context.json').write_text(json.dumps({'trial':True}))
            elif mode=='cancelled':
                store.move(1,'cancel','cancelled','User cancelled')
            before=receipt.read_bytes()
            state=store.get(1)['status']
            finish(store,1,Reply('Outcome',task_outcome=result['task_outcome']),commit=False)
            self.assertEqual(store.get(1)['status'],state)
            self.assertFalse((store.directory(1)/'completion.json').exists())
            if mode in ('decision','cancelled'):
                continue  # the normal boundary ends the turn before another model response
            answer,wires,messages=self.scripted(agent,[result])
            self.assertEqual(len(wires),1,mode)
            self.assertEqual(receipt.read_bytes(),before)

    def test_unrepaired_failure_stops_and_cannot_pass(self):
        agent,artifact,receipt=self.prepared()
        artifact.write_text('wrong',encoding='utf-8')
        done=dict(reply='Done',task_outcome='completed')
        result,wires,messages=self.scripted(agent,[done,done])
        self.assertEqual(len(wires),2)
        self.assertEqual(agent._background.finish(1,result),'needs_attention')
        self.assertNotIn('Runtime feedback',json.dumps(messages))

    def test_failed_check_repair_keeps_other_proof_and_rechecks_at_commit(self):
        from tabularium.core import turn_loop
        from tabularium.records import verification
        agent,artifact,receipt=self.prepared()
        before=receipt.read_bytes()
        other=artifact.parent/'other.txt'
        other.write_text('wrong',encoding='utf-8')
        agent._background.checkpoint(1,1,'','',[],guesses=[],completion_items=[{
            'id':2,'item':'Other correct','check':'Read other bytes',
            'verification':{'kind':'contains','paths':[str(other)],'text':'correct'}}])
        agent._execute_tool_unguarded('verify_requirement',{'task_id':1,'item':2},1)
        done=dict(reply='Done',task_outcome='completed')
        def repair():
            other.write_text('correct',encoding='utf-8')
            return turn_loop.Exchange(blocks=[{'type':'tool_use','id':'verify2','name':'verify_requirement','input':{'task_id':1,'item':2}}],
                                     calls=[('verify2','verify_requirement',{'task_id':1,'item':2})])
        result,wires,messages=self.scripted(agent,[done,repair,done])
        self.assertEqual(verification.status(agent._background,1,'',2),'passed')
        self.assertEqual(receipt.read_bytes(),before)
        self.assertEqual(agent._background.get(1)['status'],'running')
        # A changed deliverable between preview and commit cannot reuse the earlier verdict.
        other.write_text('changed after final response',encoding='utf-8')
        self.assertEqual(agent._background.finish(1,result),'needs_attention')

    def test_changing_failures_cannot_create_an_unbounded_correction_loop(self):
        from tabularium.core import turn_loop
        agent,artifact,receipt=self.prepared()
        artifact.write_text('wrong',encoding='utf-8')
        done=dict(reply='Done',task_outcome='completed')
        def another_failure():
            agent._background.checkpoint(1,1,'','',[],guesses=[],completion_items=[{
                'id':2,'item':'Changed requirement','check':'Read saved bytes',
                'verification':{'kind':'contains','paths':[str(artifact)],'text':'correct'}}])
            return turn_loop.Exchange(text=json.dumps(done))
        result,wires,messages=self.scripted(agent,[done,another_failure,dict(reply='Saved')])
        self.assertEqual(len(wires),3)
        self.assertEqual(agent._background.finish(1,result),'needs_attention')
    def test_session_limit_does_not_reset_failed_completion_budget(self):
        from tabularium.core import turn_loop
        agent,artifact,receipt=self.prepared()
        artifact.write_text('wrong',encoding='utf-8')
        agent.MAX_TOOL_ITERATIONS=2
        done=dict(reply='Done',task_outcome='completed')
        def inspect():
            args={'path':str(artifact)}
            return turn_loop.Exchange(blocks=[{'type':'tool_use','id':'inspect','name':'read_file','input':args}],
                                     calls=[('inspect','read_file',args)])
        result,wires,messages=self.scripted(agent,[done,inspect,done])
        self.assertEqual(len(wires),3)
        self.assertTrue((agent.RUNTIME_DIR/'task_state/session_checkpoint.json').exists())
        self.assertEqual(agent._background.finish(1,result),'needs_attention')
