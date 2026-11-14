"""Offline route order, billing and handoff checks; no live providers or messages."""
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch
from tabularium.models import provider_routing as routing
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
        with patch('tabularium.models.subscription_runtime.run_claude',side_effect=routing.Unavailable('quota',3600,quota=True)) as claude:
            self.assertEqual(routing.run(self.agent,history,1),'done')
        self.assertEqual(claude.call_count,1)
        self.assertEqual(len(history),2)
        self.assertIsNone(routing.ACTIVE.get())
        self.assertEqual(self.agent.send_message.call_count, 2)
        for call in self.agent.send_message.call_args_list:
            self.assertIn('usage limit reached', call.args[1])

    def test_unknown_outcome_never_falls_back(self):
        self.agent.run_turn_openai.side_effect=RuntimeError('tool outcome unknown')
        with patch('tabularium.models.subscription_runtime.run_claude') as claude:
            with self.assertRaisesRegex(RuntimeError,'unknown'):
                routing.run(self.agent,[],1)
            claude.assert_not_called()
        self.assertIsNone(routing.ACTIVE.get())

    def test_cooldown_skips_and_expiry_restores_preferred(self):
        self.agent.run_turn_openai.side_effect=routing.Unavailable('quota',60)
        with patch('tabularium.models.subscription_runtime.run_claude',return_value='Claude'):
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


class Override(unittest.TestCase):
    """A temporary route goes first without rewriting routing.json; a bad or expired one is ignored."""
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.agent = NS(AGENT_DIR=self.root, RUNTIME_DIR=self.root, _review_counts={},
                        _strip_old_images=Mock(), OPENAI_API_KEY='fake', ANTHROPIC_API_KEY='fake',
                        send_message=Mock(), run_turn_anthropic=Mock(return_value='Claude API'),
                        run_turn_openai=Mock(return_value='OpenAI'))
        # Vit's permanent order (2026-11-14)
        self.routes = [{'kind': 'anthropic_subscription', 'model': 'claude-perm'},
                       {'kind': 'openai_subscription', 'model': 'gpt-perm'},
                       {'kind': 'anthropic_api', 'model': 'claude-perm'},
                       {'kind': 'openai_api', 'model': 'gpt-perm'}]
        routing.save(self.root, {'enabled': True, 'routes': self.routes})
        self.claude = patch('tabularium.models.subscription_runtime.run_claude', return_value='Claude')
        self.run_claude = self.claude.start()
        self.addCleanup(self.claude.stop)

    def file(self):
        return self.root/'routing_override.json'

    def test_override_first_then_permanent_order_without_its_kind(self):
        routing.set_override(self.root, 'openai_subscription', 'gpt-6.1-sol', 'Alex: switch to OpenAI')
        self.assertEqual(routing.effective_routes(self.root),
                         [{'kind': 'openai_subscription', 'model': 'gpt-6.1-sol'}, self.routes[0], self.routes[2], self.routes[3]])
        seen = []
        self.agent.run_turn_openai.side_effect = lambda m, c: seen.append(dict(routing.ACTIVE.get())) or 'OpenAI'
        self.assertEqual(routing.run(self.agent, [], 1), 'OpenAI')
        self.assertEqual(seen, [{'kind': 'openai_subscription', 'model': 'gpt-6.1-sol'}])
        self.run_claude.assert_not_called()

    def test_kind_absent_from_sequence_is_added_first(self):
        routing.save(self.root, {'enabled': True, 'routes': self.routes[:1]})
        routing.set_override(self.root, 'openai_api', 'gpt-6.1-sol', '')
        self.assertEqual(routing.effective_routes(self.root),
                         [{'kind': 'openai_api', 'model': 'gpt-6.1-sol'}, self.routes[0]])

    def test_override_route_falls_back_and_cools_down(self):
        routing.set_override(self.root, 'openai_subscription', 'gpt-6.1-sol', 'switch', time.time()+3600)
        self.agent.run_turn_openai.side_effect = routing.Unavailable('quota', 3600, quota=True)
        self.assertEqual(routing.run(self.agent, [], 1), 'Claude')
        self.assertIn('Continuing through your configured fallback', self.agent.send_message.call_args.args[1])
        cooldown = routing.read(self.root/'task_state/route_openai_subscription.json')
        self.assertEqual(cooldown['model'], 'gpt-6.1-sol')
        self.agent.run_turn_openai.reset_mock(side_effect=True)
        self.assertEqual(routing.run(self.agent, [], 1), 'Claude')   # cooled-down override is skipped
        self.agent.run_turn_openai.assert_not_called()

    def test_expired_invalid_or_corrupt_override_is_ignored_without_raising(self):
        bad = [{'kind': 'openai_subscription', 'model': 'gpt-6.1-sol', 'reason': '', 'set': 1, 'expires': time.time()-1},
               {'kind': 'openai_subscription', 'model': 'claude-wrong', 'reason': '', 'set': 1, 'expires': None},
               {'kind': 'nobody', 'model': 'gpt-6.1-sol', 'reason': '', 'set': 1, 'expires': None},
               {'kind': 'openai_api', 'model': 'gpt-6.1-sol', 'reason': '', 'set': 1, 'expires': 'tomorrow'},
               ['not', 'an', 'object']]
        for value in bad:
            routing.atomic_json(self.file(), value)
            self.assertIsNone(routing.override(self.root))
            self.assertEqual(routing.effective_routes(self.root), self.routes)
        self.file().write_text('{not json', encoding='utf-8')
        self.assertEqual(routing.effective_routes(self.root), self.routes)
        self.assertEqual(routing.run(self.agent, [], 1), 'Claude')
        self.assertEqual(self.run_claude.call_args.args[3], 'claude-perm')

    def test_expiry_lapses_back_to_permanent_order(self):
        routing.set_override(self.root, 'openai_subscription', 'gpt-6.1-sol', 'switch', time.time()+60)
        self.assertIsNotNone(routing.override(self.root))
        self.assertIsNone(routing.override(self.root, now=time.time()+61))

    def test_set_and_clear_never_touch_routing_json(self):
        before = (self.root/'routing.json').read_bytes()
        stored = routing.set_override(self.root, 'openai_subscription', 'gpt-6.1-sol', 'switch')
        self.assertEqual(routing.read(self.file()), stored)
        self.assertIsNone(stored['expires'])
        self.assertEqual((self.root/'routing.json').read_bytes(), before)
        self.assertEqual(routing.config(self.root)['routes'], self.routes)
        routing.clear_override(self.root)
        routing.clear_override(self.root)   # clearing twice is harmless
        self.assertFalse(self.file().exists())
        self.assertEqual((self.root/'routing.json').read_bytes(), before)
        self.assertEqual(routing.effective_routes(self.root), self.routes)

    def test_set_rejects_bad_values_and_writes_nothing(self):
        for args in [('openai_subscription', 'claude-x', ''), ('unknown', 'gpt-x', ''),
                     ('openai_api', 'gpt-x', None), ('openai_api', 'gpt-x', '', time.time()-5), ('openai_api', 'gpt-x', '', True)]:
            self.assertRaises(ValueError, routing.set_override, self.root, *args)
        self.assertFalse(self.file().exists())

    def test_label_names_the_end(self):
        self.assertEqual(routing.override_label({'kind': 'openai_subscription', 'model': 'gpt-6.1-sol', 'expires': None}),
                         'Temporary override: gpt-6.1-sol \u00b7 ChatGPT subscription (until cleared)')
        self.assertIn('(until 20', routing.override_label({'kind': 'openai_api', 'model': 'gpt-x', 'expires': time.time()+60}))


class ClaudeBridge(unittest.TestCase):
    def test_only_owned_tools_and_durable_receipts(self):
        from tabularium.models import subscription_runtime as bridge
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
            self.assertEqual(captured['options'].output_format['schema']['required'],['reply', 'modality', 'work', 'task_outcome', 'question', 'decision_task_id', 'decision_id', 'status_claims', 'correction', 'answers', 'request', 'not_correction'])   # declared reply field
            self.assertEqual(captured['options'].env['ANTHROPIC_API_KEY'],'')
            self.assertEqual(captured['options'].allowed_tools,['mcp__agent__read_memory'])
            self.assertEqual(messages[1]['content'][0]['id'],messages[2]['content'][0]['tool_use_id'])
            self.assertEqual(messages[2]['content'][0]['content'],'Completed, receipt 123')
            agent.execute_tool.assert_called_once()
            self.assertEqual(agent.save_conversation.call_count,2)  # receipts persisted; final draft waits for verification

    def test_api_login_is_not_accepted_as_subscription(self):
        from tabularium.models import subscription_runtime as bridge
        agent=NS()
        with patch.object(bridge,'find_cli',return_value='fake.exe'),patch.object(bridge,'long_lived_token',return_value=''),\
             patch.object(bridge,'auth_status',return_value={'loggedIn':True,'authMethod':'api_key'}):   # no long-lived token on this machine either
            with self.assertRaises(routing.Unavailable):
                bridge.run_claude(agent,[],1,'claude-test')


if __name__=='__main__':
    unittest.main()
