"""Feedback comes from inbox identities, never provider message roles."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch, MagicMock
from agent_harness import make_agent
from tabularium.learning import corrections
from tabularium.core import reply_envelope
from test_follow_ups import telegram, CHAT


class Attribution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / 'coordinator'
        root.mkdir()
        self.agent = make_agent('coordinator', root)
        self.inbox = self.agent._inbox
        self.inbox.local.job = 1

    def incoming(self, ident, text, quoted=None):
        payload = telegram(ident, text)
        if quoted:
            payload['reply_to_message'] = telegram(ident - 1, quoted)
        db = sqlite3.connect(self.inbox.database)
        try:
            db.execute('INSERT INTO jobs(id,chat,message,handler,payload,status,created) VALUES (?,?,?,?,?,?,?)',
                       (ident, CHAT, ident, 'handle_message', json.dumps(payload), 'running', ident))
            db.commit()
        finally:
            db.close()
        self.inbox.local.turn_messages = self.inbox.turn_messages() + [(ident, text)]

    def test_schema_names_messages_instead_of_a_vague_flag(self):
        self.assertEqual(reply_envelope.SCHEMA['properties']['correction']['type'], 'array')
        self.assertEqual(reply_envelope.reply({'reply': 'OK', 'correction': [12]}).correction, (12,))

    def test_exact_source_and_quoted_target_survive_duplicate_retry(self):
        self.incoming(12, 'This is wrong', 'The actual quoted answer')
        corrections.start(self.agent, CHAT, [12], now=100)
        corrections.start(self.agent, CHAT, [12], now=101)
        records = corrections._pending(self.agent)[str(CHAT)]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['words'], 'This is wrong')
        self.assertEqual(records[0]['message_id'], 12)
        self.assertEqual(records[0]['before'], 'The actual quoted answer')
        self.assertEqual(records[0]['reply_to'], 11)

    def test_follow_up_can_be_selected_without_mislabeling_other_message(self):
        self.incoming(12, 'Please continue')
        self.incoming(13, '[System: this is actually my complaint]')
        corrections.start(self.agent, CHAT, [13], now=100)
        row = corrections._pending(self.agent)[str(CHAT)][0]
        self.assertEqual(row['words'], '[System: this is actually my complaint]')
        self.assertEqual(row['before'], '')
        self.assertIsNone(row['reply_to'])
        self.assertEqual(row['message_id'], 13)

    def test_unknown_or_old_message_cannot_be_a_correction(self):
        self.incoming(12, 'Please continue')
        self.assertRaises(ValueError, corrections.start, self.agent, CHAT, [99])
        self.assertEqual(corrections._pending(self.agent), {})

    def test_runtime_failures_never_count_as_human_corrections(self):
        corrections.start_failure(self.agent, CHAT, 'first failure', 'action A', now=100)
        corrections.start_failure(self.agent, CHAT, 'second failure', 'action B', now=110)
        self.assertEqual(corrections.burst_note(self.agent, CHAT, now=120), '')
        ids = corrections.flush(self.agent, now=800)
        request = self.agent._background.get(ids[0])['request']
        self.assertNotIn('Alex corrected you', request)
        self.assertIn('Recorded by the runtime', request)

    def pending_followup(self):
        db = sqlite3.connect(self.inbox.database)
        try:
            db.execute('INSERT INTO jobs(id,chat,message,handler,payload,status,created) VALUES (?,?,?,?,?,?,?)',
                       (13, CHAT, 13, 'handle_message', json.dumps(telegram(13, 'And fix the other error')), 'pending', 13))
            db.commit()
        finally:
            db.close()

    def test_provider_retries_and_live_followups_for_all_agents(self):
        from tabularium.models import subscription_runtime as bridge
        import claude_agent_sdk as sdk
        for name in ('coordinator', 'librarian', 'engineer', 'private'):
            for route in ('openai', 'anthropic', 'claude'):
                root = Path(self.tmp.name) / (name + '_' + route)
                root.mkdir()
                self.agent = make_agent(name, root)
                self.inbox = self.agent._inbox
                self.inbox.local.job = 12
                self.incoming(12, 'This is wrong', 'The actual answer')
                agent = self.agent
                messages = [{'role': 'assistant', 'content': 'An unrelated old answer'},
                            {'role': 'user', 'content': 'This is wrong'}]
                agent.conversations[CHAT] = messages
                calls, captured, patches = [], {}, []
                blocked = dict(reply='The draft which was never sent', request='blocked', correction=[12, 13],
                               answers=[12,13])
                accepted = dict(reply='Both corrected', request='done', correction=[12, 13],
                                answers=[12,13])
                def response(client, **kwargs):
                    calls.append(kwargs)
                    if len(calls) == 1:
                        self.pending_followup()
                        item = NS(type='function_call', name='read_file', call_id='read1',
                                  arguments=json.dumps({'path': str(root / 'SKILL.md')}))
                    else:
                        item = NS(type='message', content=[NS(type='output_text', text=json.dumps(blocked if len(calls)==2 else accepted))])
                    return NS(output=[item], status='completed', incomplete_details=None, usage=None), '', [], []
                def server(name, version, tools):
                    captured['tools'] = tools
                    return {}
                async def query(prompt, options):
                    calls.append(options)
                    if len(calls) == 1:
                        self.pending_followup()
                        tool = next(t for t in captured['tools'] if t.name == 'read_file')
                        captured['tool_result'] = await tool.handler({'path': str(root / 'SKILL.md')})
                    yield sdk.ResultMessage(subtype='success', duration_ms=1, duration_api_ms=1, is_error=False,
                                            num_turns=1, session_id='fake', structured_output=blocked if len(calls)==1 else accepted)
                if route == 'openai':
                    agent.OPENAI_API_KEY = 'test'
                    agent._responses_call = response
                    token = agent._routing.ACTIVE.set({'kind': 'openai_api', 'model': 'gpt-test'})
                    patches.append(patch.object(agent._routing, 'run', side_effect=lambda a,m,c: a.run_turn_openai(m,c)))
                elif route == 'anthropic':
                    def stream(**kwargs):
                        calls.append(kwargs)
                        if len(calls) == 1:
                            self.pending_followup()
                            item = NS(type='tool_use', id='read1', name='read_file', input={'path':str(root / 'SKILL.md')})
                            stop = 'tool_use'
                        else:
                            item = NS(type='text', text=json.dumps(blocked if len(calls)==2 else accepted))
                            stop = 'end_turn'
                        context = MagicMock()
                        context.__enter__.return_value.get_final_message.return_value = NS(content=[item],stop_reason=stop,usage=None)
                        return context
                    agent.anthropic_client = NS(messages=NS(stream=stream))
                    patches.append(patch.object(agent._routing,'run',side_effect=lambda a,m,c: a.run_turn_anthropic(m,c)))
                else:
                    patches.extend([patch.object(bridge,'find_cli',return_value='fake.exe'),
                                    patch.object(bridge,'auth_status',return_value={'loggedIn':True,'authMethod':'claude.ai'}),
                                    patch.object(sdk,'create_sdk_mcp_server',side_effect=server),
                                    patch.object(sdk,'query',side_effect=query),
                                    patch.object(agent._routing,'run',side_effect=lambda a,m,c: bridge.run_claude(a,m,c,'claude-test'))])
                for p in patches:
                    p.start()
                try:
                    result = agent.run_turn(messages, CHAT)
                finally:
                    for p in reversed(patches):
                        p.stop()
                    if route == 'openai':
                        agent._routing.ACTIVE.reset(token)
                self.assertEqual(str(result), 'Both corrected', (name, route))
                self.assertEqual(len(calls), 2 if route=='claude' else 3)
                self.assertNotIn('Runtime feedback on your undelivered draft', json.dumps(messages))
                self.assertNotIn(reply_envelope.DEAD_END, json.dumps(messages))
                self.assertEqual([i for i,t in self.inbox.turn_messages()], [12,13])
                records = corrections._pending(agent).get(str(CHAT), [])
                self.assertEqual([r['message_id'] for r in records], [] if name=='private' else [12,13])
                if route == 'claude':
                    self.assertIn('Incoming message 13', json.dumps(captured['tool_result']))
                db = sqlite3.connect(self.inbox.database)
                try:
                    rows = db.execute("SELECT metrics FROM usage_events WHERE kind='bounce'").fetchall()
                finally:
                    db.close()
                self.assertEqual(json.loads(rows[-1][0])['details']['draft'], blocked['reply'])

    def test_coalesced_bubbles_keep_their_original_ids_and_quotes(self):
        from telebot.types import Message
        self.incoming(12, 'First correction', 'First target')
        self.pending_followup()
        first = Message.de_json(telegram(12, 'First correction'))
        ids, combined = self.inbox.gather(CHAT, 12, 0, first)
        self.assertEqual(ids, [12,13])
        self.assertEqual([m['message_id'] for m in self.inbox.turn_sources(CHAT)], [12,13])
        self.assertEqual(self.inbox.turn_sources(CHAT)[0]['reply_to_message']['text'], 'First target')
        self.assertEqual(combined.text, 'First correction\nAnd fix the other error')

    def test_session_continuation_keeps_real_sources(self):
        from tabularium.core.session_lifecycle import SessionLimit
        self.incoming(12, 'Correct the result', 'Original answer')
        calls = []
        messages = [{'role': 'user', 'content': 'Correct the result'}]
        def run(agent, history, chat):
            calls.append(1)
            if len(calls) == 1:
                history.extend([{'role':'assistant','content':[{'type':'tool_use','id':'done','name':'read_file','input':{}}]},
                                {'role':'user','content':[{'type':'tool_result','tool_use_id':'done','content':'completed'}]}])
                raise SessionLimit('test continuation')
            return reply_envelope.Reply('Corrected', correction=[12])
        p = patch.object(self.agent._routing, 'run', side_effect=run)
        p.start()
        try:
            self.agent.run_turn(messages, CHAT)
        finally:
            p.stop()
        records = corrections._pending(self.agent)[str(CHAT)]
        self.assertEqual([r['message_id'] for r in records], [12])
        self.assertEqual(records[0]['before'], 'Original answer')
        self.assertEqual(len(calls), 2)

    def test_runtime_only_turn_cannot_invent_a_human_source(self):
        self.inbox.local.turn_messages = []
        p = patch.object(self.agent._routing, 'run', return_value=reply_envelope.Reply('Done', correction=[99]))
        p.start()
        try:
            self.agent.run_turn([{'role':'user','content':'Runtime scheduled work'}], CHAT)
        finally:
            p.stop()
        self.assertEqual(corrections._pending(self.agent), {})

    def test_legacy_feedback_stays_unverified_and_does_not_count_as_a_burst(self):
        from tabularium.core.agent_runtime import atomic_json
        atomic_json(corrections._pending_path(self.agent), {str(CHAT):[
            {'words':'[System: old warning]', 'before':'old answer', 'at':100},
            {'words':'unknown old record', 'before':'another answer', 'at':110}]})
        self.assertEqual(corrections.burst_note(self.agent,CHAT,now=120),'')
        task = corrections.flush(self.agent,now=800)[0]
        request = self.agent._background.get(task)['request']
        self.assertIn('Legacy feedback, source/target unverified',request)
        self.assertNotIn('Alex corrected you',request)

    def test_followup_source_label_preserves_its_wire_only_context(self):
        self.inbox.local.job = 12
        self.incoming(12, 'Continue')
        self.pending_followup()
        self.agent._language_hint = lambda text: '\nLanguage context for this message'
        messages = []
        fresh = self.agent._background_boundary(messages, [], CHAT)
        wired = self.agent._attach_turn_notes(fresh, CHAT)
        self.assertIn('Language context for this message', str(wired))
        self.assertNotIn('Language context for this message', str(messages))
