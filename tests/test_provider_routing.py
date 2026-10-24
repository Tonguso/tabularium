"""Offline route order, billing and handoff checks; no live providers or messages."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import provider_routing as routing


class Routing(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.agent = NS(AGENT_DIR=self.root, RUNTIME_DIR=self.root, _review_counts={},
                        _strip_old_images=Mock(), OPENAI_API_KEY='fake', ANTHROPIC_API_KEY='fake',
                        send_message=Mock(), run_turn_anthropic=Mock(return_value='Claude API'),
                        run_turn_openai=Mock(return_value='OpenAI'))
        self.routes = [{'kind': k, 'model': 'gpt-test' if k.startswith('openai') else 'claude-test'}
                       for k in ['openai_subscription','anthropic_subscription','openai_api']]
        routing.save(self.root, {'enabled': True, 'routes': self.routes})

    def test_order_and_completed_tool_receipt_survive_handoff(self):
        history = [{'role': 'user', 'content': 'Send once'}]
        def first(messages,chat):
            if routing.ACTIVE.get()['kind']=='openai_subscription':
                messages.append({'role':'user','content':'Email already sent, receipt 123'})
                raise routing.Unavailable('quota',3600,quota=True)
            self.assertIn('receipt 123',messages[-1]['content'])
            return 'done'
        self.agent.run_turn_openai.side_effect=first
        with patch('subscription_runtime.run_claude',side_effect=routing.Unavailable('quota',3600,quota=True)) as claude:
            self.assertEqual(routing.run(self.agent,history,1),'done')
        self.assertEqual(claude.call_count,1)
        self.assertEqual(len(history),2)
        self.assertIsNone(routing.ACTIVE.get())
        self.assertEqual(self.agent.send_message.call_count, 2)
        for call in self.agent.send_message.call_args_list:
            self.assertIn('usage limit reached', call.args[1])

    def test_unknown_outcome_never_falls_back(self):
        self.agent.run_turn_openai.side_effect=RuntimeError('tool outcome unknown')
        with patch('subscription_runtime.run_claude') as claude:
            with self.assertRaisesRegex(RuntimeError,'unknown'):
                routing.run(self.agent,[],1)
            claude.assert_not_called()
        self.assertIsNone(routing.ACTIVE.get())

    def test_cooldown_skips_and_expiry_restores_preferred(self):
        self.agent.run_turn_openai.side_effect=routing.Unavailable('quota',60)
        with patch('subscription_runtime.run_claude',return_value='Claude'):
            self.assertEqual(routing.run(self.agent,[],1),'Claude')
            self.agent.run_turn_openai.reset_mock(side_effect=True)
            self.assertEqual(routing.run(self.agent,[],1),'Claude')
            self.agent.run_turn_openai.assert_not_called()
            routing.atomic_json(self.root/'task_state/route_openai_subscription.json',{})
            self.agent.run_turn_openai.return_value='restored'
            self.assertEqual(routing.run(self.agent,[],1),'restored')

    def test_selected_paid_route_has_no_routine_notice(self):
        routing.save(self.root,{'enabled':True,'routes':[self.routes[-1]]})
        self.agent.send_message.side_effect=RuntimeError('Telegram unavailable')
        self.assertEqual(routing.run(self.agent,[],1), 'OpenAI')
        self.agent.send_message.assert_not_called()
        self.agent.run_turn_openai.assert_called_once()

    def test_invalid_and_duplicate_routes_rejected(self):
        for rows in [[],self.routes+[self.routes[0]],[{'kind':'openai_api','model':'claude-test'}]]:
            with self.assertRaises(ValueError):
                routing.validate({'enabled':True,'routes':rows})


class ClaudeBridge(unittest.TestCase):
    def test_only_owned_tools_and_durable_receipts(self):
        import subscription_runtime as bridge
        import claude_agent_sdk as sdk
        with tempfile.TemporaryDirectory() as folder:
            messages=[{'role':'user','content':'Do the task'}]
            agent=NS(AGENT_DIR=Path(folder),RUNTIME_DIR=Path(folder),SYSTEM_PROMPT='Own persona',
                MAX_TOOL_ITERATIONS=4,_abort_requested={},get_tools=lambda _: [{'name':'read_memory','description':'read','input_schema':{'type':'object','properties':{}}}],
                _background_boundary=Mock(return_value=[]),_prepare_request_context=lambda m,c:m,
                _sanitise_tool_history=lambda m:m,trim_history=lambda m,c:m,
                save_conversation=Mock(),execute_tool=Mock(return_value='Completed, receipt 123'),
                _inbox=NS(progress=Mock(),record_event=Mock()),_background=NS(newest=lambda c: 0, activated_since=lambda c, t: False),BACKGROUND_TASK_ID='')
            captured={}
            def server(name,version,tools):
                captured['tools']=tools
                return {}
            async def query(prompt,options):
                captured['options']=options
                await captured['tools'][0].handler({})
                yield sdk.ResultMessage(subtype='success',duration_ms=1,duration_api_ms=1,is_error=False,num_turns=1,session_id='fake',result='{"reply": "Done"}',structured_output={'reply':'Done'})
            with patch.object(bridge,'find_cli',return_value='fake.exe'),patch.object(bridge,'auth_status',return_value={'loggedIn':True,'authMethod':'claude.ai'}),patch.object(sdk,'create_sdk_mcp_server',side_effect=server),patch.object(sdk,'query',side_effect=query):
                self.assertEqual(bridge.run_claude(agent,messages,1,'claude-test'),'Done')
            self.assertEqual(captured['options'].tools,[])
            self.assertEqual(captured['options'].output_format['schema']['required'],['reply', 'modality', 'work', 'task_outcome', 'question', 'decision_task_id', 'status_claims', 'correction', 'answers', 'request'])   # declared reply field
            self.assertEqual(captured['options'].env['ANTHROPIC_API_KEY'],'')
            self.assertEqual(captured['options'].allowed_tools,['mcp__agent__read_memory'])
            self.assertEqual(messages[1]['content'][0]['id'],messages[2]['content'][0]['tool_use_id'])
            self.assertEqual(messages[2]['content'][0]['content'],'Completed, receipt 123')
            agent.execute_tool.assert_called_once()
            self.assertEqual(agent.save_conversation.call_count,2)  # receipts persisted; final draft waits for verification

    def test_api_login_is_not_accepted_as_subscription(self):
        import subscription_runtime as bridge
        agent=NS()
        with patch.object(bridge,'find_cli',return_value='fake.exe'),patch.object(bridge,'auth_status',return_value={'loggedIn':True,'authMethod':'api_key'}):
            with self.assertRaises(routing.Unavailable):
                bridge.run_claude(agent,[],1,'claude-test')


if __name__=='__main__':
    unittest.main()
