import json, sqlite3
from pathlib import Path
import tempfile
import unittest
from tabularium.core.agent_runtime import DurableInbox
from tabularium.records import task_cost
class UsageAccounting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.inbox = DurableInbox(self.root, lambda *args: None)
    def tearDown(self):
        self.tmp.cleanup()
    def metrics(self):
        db = sqlite3.connect(self.inbox.database)
        try:
            return json.loads(db.execute('SELECT metrics FROM usage_events ORDER BY id DESC LIMIT 1').fetchone()[0])
        finally:
            db.close()
    def test_openai_cache_writes_are_disjoint_from_uncached_input(self):
        self.inbox.record_event('response', 'gpt', 'subscription', 'openai', {
            'input_tokens': 1000, 'input_tokens_details': {'cached_tokens': 400, 'cache_write_tokens': 500}, 'output_tokens': 20})
        self.assertEqual(self.metrics()['input_uncached'], 100)
        self.assertEqual(self.metrics()['cache_write'], 500)
    def test_unknown_cache_is_not_a_measured_zero(self):
        self.inbox.record_event('response', 'gpt', 'subscription', 'openai', {'input_tokens':1000, 'output_tokens':20})
        result = task_cost.spend(self.root)
        self.assertIsNone(result['cache_read'])
        self.assertEqual(result['input_total'], 1000)
        self.assertEqual(result['unknown_fields']['cache_read'], 1)
    def test_incomplete_anthropic_usage_does_not_invent_total(self):
        self.inbox.record_event('response', 'claude', 'subscription', 'claude-sdk', {'input_tokens': 10, 'output_tokens':20})
        self.assertIsNone(task_cost.spend(self.root)['input_total'])
    def test_pending_tools_do_not_multiply_model_cost(self):
        self.inbox.record_event('response', 'gpt', 'subscription', 'openai', {'input_tokens':1000, 'input_tokens_details':{'cached_tokens':0, 'cache_write_tokens':0}, 'output_tokens':20})
        before = task_cost.spend(self.root)
        for i in range(4):
            self.inbox.record_event('tool_started')
        after = task_cost.spend(self.root)
        self.assertEqual(after.get('projected_spent', after.get('estimated_effective_tokens')), before.get('effective_spent',before.get('estimated_effective_tokens')))
    def test_unknown_response_keeps_totals_unknown_and_subtotals_visible(self):
        self.inbox.record_event('response', 'gpt', 'subscription', 'openai', {'input_tokens':1000, 'output_tokens':20})
        self.inbox.record_event('response', 'gpt', 'subscription', 'openai')
        result = task_cost.spend(self.root)
        self.assertIsNone(result['input_total'])
        self.assertEqual(result['known_subtotals']['input_total'], 1000)

    def test_legacy_sdk_estimates_are_not_relabelled_as_actual_usage(self):
        self.inbox.record_event('response','claude','subscription','claude-sdk',
            dict(input_tokens=10,cache_read_input_tokens=100,cache_creation_input_tokens=20,output_tokens=1000),role='sdk_stream')
        result=task_cost.spend(self.root)
        self.assertIsNone(result['output'])
        self.assertEqual(result['input_total'],130)
        self.assertTrue(result['estimate_incomplete'])

    def test_snapshot_is_one_durable_record_and_partial_subtotal_is_visible(self):
        db=sqlite3.connect(self.inbox.database)
        try:
            db.execute("INSERT INTO jobs(id,chat,message,status,created) VALUES (1,1,1,'running',0)")
            db.commit()
        finally:
            db.close()
        self.inbox.local.job=1
        ident=self.inbox.record_event('response','claude','subscription','claude-sdk',role='sdk_total',partial=True)
        usage=dict(input_tokens=10,cache_read_input_tokens=100,cache_creation_input_tokens=20)
        self.inbox.record_event('response','claude','subscription','claude-sdk',usage,role='sdk_total',event_id=ident,partial=True)
        result=task_cost.spend(self.root)
        self.assertEqual(result['turns'],1)
        self.assertIsNone(result['input_total'])
        self.assertEqual(result['known_subtotals']['input_total'],130)
        self.assertIn('known subtotal 130',self.inbox.usage_summary(1))
        self.assertEqual(result['estimated_effective_tokens'],45)
        self.inbox.record_event('tool_started')
        self.inbox.record_event('response','claude','subscription','claude-sdk',dict(usage,output_tokens=50),role='sdk_total',event_id=ident)
        result=task_cost.spend(self.root)
        self.assertEqual((result['turns'],result['input_total'],result['output']),(1,130,50))
        self.assertEqual(result['unknown_usage'],0)
        self.assertEqual(result['pending_tools'],0)
        self.assertFalse(result['estimate_incomplete'])


class SDKAccounting(unittest.TestCase):
    def test_final_usage_replaces_provisional_text_estimate_for_every_agent(self):
        from unittest.mock import patch
        import claude_agent_sdk as sdk
        from tabularium.models import subscription_runtime as bridge
        from agent_harness import make_agent
        from tabularium.core.roster import ROLES
        tmp = tempfile.TemporaryDirectory()
        try:
            for name, interrupted in [(n, stopped) for n in ROLES for stopped in (False,True)]:
                folder = Path(tmp.name)/(name+str(interrupted))
                folder.mkdir()
                agent = make_agent(name, folder, background=True)
                agent._background.checkpoint(1,1,'','',[],guesses=[],completion_items=[
                    {'item':'Return accounting fixture','check':'Read response'}])
                agent._load_core_memory = lambda: ''
                messages = [{'role':'user','content':'Synthetic accounting test'}]
                agent.conversations[1] = messages
                async def query(prompt, options):
                    yield sdk.AssistantMessage(content=[sdk.TextBlock(text='x'*4000)],model='test',
                        message_id='one',usage=dict(input_tokens=10,cache_read_input_tokens=100,cache_creation_input_tokens=20,output_tokens=3))
                    if interrupted:
                        raise RuntimeError('synthetic connection lost')
                    yield sdk.ResultMessage(subtype='success',duration_ms=1,duration_api_ms=1,is_error=False,
                        num_turns=1,session_id='test',structured_output={'reply':'done','task_outcome':'completed'},
                        usage=dict(input_tokens=10,cache_read_input_tokens=100,cache_creation_input_tokens=20,output_tokens=50))
                patches=[patch.object(bridge,'find_cli',return_value='fake.exe'),
                         patch.object(bridge,'subscription_ready',return_value=(True,'')),
                         patch.object(sdk,'query',side_effect=query)]
                for item in patches: item.start()
                try:
                    if interrupted:
                        try:
                            bridge.run_claude(agent,messages,1,'test')
                        except RuntimeError as exc:
                            self.assertIn('synthetic connection lost',str(exc))
                        else:
                            self.fail('Expected interrupted SDK session')
                    else:
                        bridge.run_claude(agent,messages,1,'test')
                    result=task_cost.spend(agent.RUNTIME_DIR)
                    self.assertEqual(result['known_subtotals']['input_total'],130,name)
                    self.assertEqual(result['turns'],1,name)
                    self.assertEqual(result['output'],None if interrupted else 50,name)
                    self.assertEqual(result['input_total'],None if interrupted else 130,name)
                finally:
                    for item in reversed(patches): item.stop()
        finally:
            tmp.cleanup()

    def test_duplicates_crash_totals_and_waiting_keep_only_trustworthy_counts(self):
        from unittest.mock import patch
        import asyncio
        import claude_agent_sdk as sdk
        from tabularium.models import subscription_runtime as bridge
        from tests.agent_harness import make_agent
        temp=tempfile.TemporaryDirectory()
        try:
            for mode in ('duplicate','zeroed','missing','waiting'):
                root=Path(temp.name)/mode;root.mkdir()
                agent=make_agent('cato',root,background=True)
                agent._background.checkpoint(1,1,'','',[],guesses=[],completion_items=[{'item':'Fixture','check':'Read reply'}])
                captured={}
                def server(name,version,tools):
                    captured['tools']=tools
                    return {}
                async def query(prompt,options):
                    usage=dict(input_tokens=10,cache_read_input_tokens=100,cache_creation_input_tokens=20,output_tokens=999999)
                    for ident,parent in [('one',None),('one',None),('child','tool-parent'),(None,None)]:
                        yield sdk.AssistantMessage(content=[],model='test',message_id=ident,parent_tool_use_id=parent,usage=usage)
                    if mode=='waiting':
                        tool=next(t for t in captured['tools'] if t.name=='task_decision')
                        await tool.handler({'action':'ask','task_id':1,'question':'GBP or USD?'})
                        try:
                            await asyncio.Event().wait()
                        finally:
                            captured['cancelled']=True
                    elif mode=='missing':
                        raise RuntimeError('synthetic interruption')
                    else:
                        totals=dict(input_tokens=0,cache_read_input_tokens=0,cache_creation_input_tokens=0,output_tokens=0) if mode=='zeroed' else dict(usage,output_tokens=50)
                        yield sdk.ResultMessage(subtype='success',duration_ms=1,duration_api_ms=1,is_error=False,num_turns=1,
                                                session_id='fixture',usage=totals,structured_output={'reply':'Done','task_outcome':'completed'})
                patches=[patch.object(bridge,'find_cli',return_value='fake.exe'),
                         patch.object(bridge,'subscription_ready',return_value=(True,'')),
                         patch.object(sdk,'create_sdk_mcp_server',side_effect=server),
                         patch.object(sdk,'query',side_effect=query),
                         patch.object(agent._routing,'run',side_effect=lambda a,m,c:bridge.run_claude(a,m,c,'test'))]
                for p in patches:p.start()
                try:
                    if mode=='missing':
                        self.assertRaisesRegex(RuntimeError,'synthetic interruption',agent.run_turn,[{'role':'user','content':'Fixture'}],1)
                    else:
                        result=agent.run_turn([{'role':'user','content':'Fixture'}],1)
                        if mode=='waiting':
                            self.assertEqual(result.task_outcome,'waiting')
                            self.assertEqual(agent._background.get(1)['status'],'needs_answer')
                            self.assertTrue(captured['cancelled'])
                    usage=task_cost.spend(agent.RUNTIME_DIR)
                    self.assertEqual(usage['turns'],1,mode)
                    self.assertEqual(usage['known_subtotals']['input_total'],130,mode)
                    self.assertEqual(usage['output'],50 if mode=='duplicate' else None,mode)
                    self.assertEqual(usage['known_subtotals']['output'],50 if mode=='duplicate' else 0,mode)
                finally:
                    for p in reversed(patches):p.stop()
        finally:
            temp.cleanup()

    def test_hard_process_exit_retains_input_snapshot(self):
        import subprocess,sys
        temp=tempfile.TemporaryDirectory()
        try:
            root=Path(temp.name)/'cato';root.mkdir()
            script=Path(temp.name)/'crash.py'
            script.write_text("""import os,sys
from pathlib import Path
from unittest.mock import patch
from tests.agent_harness import make_agent
from tabularium.models import subscription_runtime as bridge
import claude_agent_sdk as sdk
agent=make_agent('cato',Path(sys.argv[1]),background=True)
async def query(prompt,options):
    yield sdk.AssistantMessage(content=[],model='test',message_id='one',usage=dict(input_tokens=10,cache_read_input_tokens=100,cache_creation_input_tokens=20,output_tokens=999))
    os._exit(7)
patch.object(bridge,'find_cli',return_value='fake.exe').start()
patch.object(bridge,'subscription_ready',return_value=(True,'')).start()
patch.object(sdk,'query',side_effect=query).start()
bridge.run_claude(agent,[{'role':'user','content':'Fixture'}],1,'test')
""",encoding='utf-8')
            result=subprocess.run([sys.executable,'-B',str(script),str(root)],capture_output=True,timeout=30)
            self.assertEqual(result.returncode,7,result.stderr.decode(errors='replace'))
            usage=task_cost.spend(root/'task_state/background/1')
            self.assertEqual(usage['turns'],1)
            self.assertEqual(usage['known_subtotals']['input_total'],130)
            self.assertIsNone(usage['input_total'])
            self.assertIsNone(usage['output'])
        finally:
            temp.cleanup()


if __name__ == '__main__':
    unittest.main()

