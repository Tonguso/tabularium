import json, sqlite3
from pathlib import Path
import tempfile
import unittest
from agent_runtime import DurableInbox
import task_cost

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

class SDKAccounting(unittest.TestCase):
    def test_final_usage_replaces_provisional_text_estimate_for_every_agent(self):
        from unittest.mock import patch
        import claude_agent_sdk as sdk
        import subscription_runtime as bridge
        from agent_harness import make_agent
        from agent_core.roster import ROLES
        tmp = tempfile.TemporaryDirectory()
        try:
            for name, interrupted in [(n, stopped) for n in ROLES for stopped in (False,True)]:
                folder = Path(tmp.name)/(name+str(interrupted))
                folder.mkdir()
                agent = make_agent(name, folder, background=True)
                agent._load_core_memory = lambda: ''
                messages = [{'role':'user','content':'Synthetic accounting test'}]
                agent.conversations[1] = messages
                async def query(prompt, options):
                    yield sdk.AssistantMessage(content=[sdk.TextBlock(text='x'*4000)],model='test',
                        message_id='one',usage=dict(input_tokens=10,cache_read_input_tokens=100,cache_creation_input_tokens=20,output_tokens=3))
                    if interrupted:
                        raise RuntimeError('synthetic connection lost')
                    yield sdk.ResultMessage(subtype='success',duration_ms=1,duration_api_ms=1,is_error=False,
                        num_turns=1,session_id='test',structured_output={'reply':'done'},
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
                    self.assertEqual(result['output'],None if interrupted else 50,name)
                    self.assertEqual(result['input_total'],None if interrupted else 130,name)
                finally:
                    for item in reversed(patches): item.stop()
        finally:
            tmp.cleanup()


if __name__ == '__main__':
    unittest.main()

