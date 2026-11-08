"""Dashboard requests exercise the shared queue and real handler, never live services."""
import json
import tempfile
import sqlite3
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from tabularium.core.agent_runtime import DurableInbox
from tabularium.channels.owner_telegram import OWNER_CHAT_ID


class DashboardMessaging(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.inbox = DurableInbox(self.root, Mock())

    def test_dashboard_retry_is_one_job_and_id_cannot_change_content(self):
        first = self.inbox.accept_dashboard('request-1', 'Hello')
        again = self.inbox.accept_dashboard('request-1', 'Hello')
        self.assertEqual(first, again)
        self.assertLess(first['message_id'], 0)
        self.assertRaises(ValueError, self.inbox.accept_dashboard, 'request-1', 'Different')
        self.assertRaises(ValueError, self.inbox.accept_dashboard, 'empty', '   ')

    def test_dashboard_order_uses_queue_order_not_platform_numbers(self):
        first = self.inbox.accept_dashboard('request-1', 'First')
        self.inbox.accept_dashboard('request-2', 'Second')
        self.assertTrue(self.inbox.superseded(OWNER_CHAT_ID, first['message_id']))
        self.assertEqual(self.inbox.destination(OWNER_CHAT_ID, first['message_id']), 'dashboard')

    def test_reply_is_not_absorbed_as_an_unbound_followup(self):
        first = self.inbox.accept_dashboard('request-1', 'First')
        self.inbox.accept_dashboard('answer-1', 'Yes', decision_id=9)
        self.inbox.accept_dashboard('followup-1', 'Include the appendix')
        followups = self.inbox.follow_ups(OWNER_CHAT_ID, first['job'])
        self.assertEqual([m.text for _, m in followups], ['Include the appendix'])

    def test_outbox_dashboard_reply_never_calls_telegram_and_retains_plain_text(self):
        from tabularium.records.outbox import Outbox
        from tabularium.channels.messaging import conversation
        bot = Mock(side_effect=AssertionError('Telegram must not be called'))
        outbox = Outbox(self.root, bot)
        outbox.put(OWNER_CHAT_ID, ['Dashboard reply'], decision=12, channel='dashboard', original='**Dashboard reply**')
        rows = conversation(self.root, OWNER_CHAT_ID)['messages']
        self.assertEqual(rows[-1]['text'], '**Dashboard reply**')
        self.assertEqual(rows[-1]['decision_id'], 12)
        self.assertEqual(rows[-1]['channel'], 'dashboard')
        self.assertFalse(outbox.pending())
        bot.assert_not_called()

    def test_chunked_telegram_reply_is_one_dashboard_message_without_copied_originals(self):
        from tabularium.records.outbox import Outbox
        from tabularium.channels.messaging import conversation
        outbox = Outbox(self.root, lambda:Mock())
        original='Long reply across many chunks'
        outbox.put(OWNER_CHAT_ID, ['chunk']*110, original=original)
        messages=conversation(self.root,OWNER_CHAT_ID)['messages']
        self.assertEqual([m['text'] for m in messages], [original])
        db=sqlite3.connect(outbox.path)
        try:
            size=db.execute('SELECT sum(length(original)) FROM outbox').fetchone()[0]
        finally:
            db.close()
        self.assertEqual(size,len(original))

    def test_all_agents_handle_dashboard_without_typing_or_voice_network_calls(self):
        from tests.agent_harness import make_agent
        from tabularium.channels.messaging import decode
        from tabularium.core.reply_envelope import Reply
        from tabularium.core.roster import AGENTS
        for name in AGENTS:
            root = self.root / name
            root.mkdir()
            a = make_agent(name, root)
            a.bot = Mock()
            a._run_turn_with_guard = Mock(return_value=Reply('Received'))
            a.maybe_speak = Mock(side_effect=AssertionError('No voice network call'))
            # The harness initially constructs a worker inbox; give the foreground its real inbox.
            a._inbox = DurableInbox(root, Mock())
            receipt = a._inbox.accept_dashboard('input-1', 'Hello from dashboard')
            a._inbox.local.message_id = receipt['message_id']
            a._inbox.local.turn_messages = [(receipt['message_id'], 'Hello from dashboard')]
            connection = sqlite3.connect(a._inbox.database)
            try:
                payload = connection.execute('SELECT payload FROM jobs WHERE id=?', (receipt['job'],)).fetchone()[0]
            finally:
                connection.close()
            a.handle_message(decode(json.loads(payload)))
            a._run_turn_with_guard.assert_called_once()
            a.bot.send_chat_action.assert_not_called()
            a.bot.send_message.assert_not_called()

    def test_explicit_action_answer_runs_once_unrelated_yes_never_approves(self):
        from tests.agent_harness import make_agent
        from tabularium.channels.messaging import decode
        from tabularium.records import task_decisions as td
        a = make_agent('cato', self.root)
        a.send_message = Mock()
        a.execute_tool = Mock(return_value='done')
        d = td.park_action(a._background, OWNER_CHAT_ID, 'execute_command', {'command':'echo test'}, 'Run this exact command?')
        raw = {'channel':'dashboard','message_id':-1,'date':time.time(),'text':'yes'}
        self.assertFalse(a._settle_by_reply(decode(raw))[0])
        a.execute_tool.assert_not_called()
        raw['decision_id'] = d['id']
        self.assertTrue(a._settle_by_reply(decode(raw))[0])
        self.assertTrue(a._settle_by_reply(decode(raw))[0])
        a.execute_tool.assert_called_once_with('execute_command', {'command':'echo test'}, OWNER_CHAT_ID)

    def test_a_skill_edit_verdict_from_the_card_is_settled_before_any_turn(self):
        """2026-11-08: the Apply and Drop buttons are his answer; the runtime records it, no agent turn is needed."""
        from tests.agent_harness import make_agent
        from tabularium.channels.messaging import decode
        from tabularium.learning import learning
        a = make_agent('cato', self.root)
        a.send_message = Mock()
        skill = a.AGENT_DIR / 'skills' / 'basket' / 'SKILL.md'
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text('---\nname: basket\nmetadata:\n  version: "1.0.0"\n---\n\n## Rules\n- Read the archives.\n', encoding='utf-8')
        key = learning.propose(a.AGENT_DIR, OWNER_CHAT_ID, 'Freeze membership first', 't1', 'basket_build')
        learning.propose_change(a.AGENT_DIR, OWNER_CHAT_ID, key, skill, '- Read the archives.', '- Freeze the membership, then read the archives.')
        raw = {'channel':'dashboard','message_id':-1,'date':time.time(),'text':f'Skill edit {key}: apply it.'}
        self.assertTrue(a._settle_by_reply(decode(raw))[0])
        self.assertIn('applied to SKILL.md', a.send_message.call_args[0][1])
        self.assertIn('Freeze the membership', skill.read_text(encoding='utf-8'))
        self.assertEqual(learning.proposals(a.AGENT_DIR, OWNER_CHAT_ID), [])
        self.assertEqual(a.conversations[OWNER_CHAT_ID][-1]['role'], 'assistant')          # Cato reads the outcome next turn
        raw = {'channel':'dashboard','message_id':-2,'date':time.time(),'text':f'Skill edit {key}: drop it.'}
        self.assertTrue(a._settle_by_reply(decode(raw))[0])
        self.assertIn('retired. This lesson changed a rule in place', a.send_message.call_args[0][1])   # his drop of an applied edit

    def test_new_channel_does_not_block_behind_failed_telegram_delivery(self):
        from tabularium.records.outbox import Outbox
        from tabularium.channels.messaging import conversation
        import requests
        bot = Mock()
        bot.send_message.side_effect = requests.ConnectionError('offline')
        outbox = Outbox(self.root, lambda:bot)
        outbox.put(OWNER_CHAT_ID, ['Telegram pending'])
        outbox.put(OWNER_CHAT_ID, ['Dashboard available'], channel='dashboard', original='Dashboard available')
        self.assertEqual(len(outbox.pending()), 1)
        self.assertEqual(conversation(self.root,OWNER_CHAT_ID)['messages'][-1]['text'], 'Dashboard available')

    def test_source_routing_survives_background_delivery_without_current_turn(self):
        from tests.agent_harness import make_agent
        from tabularium.channels.messaging import conversation
        a = make_agent('cato', self.root)
        a._inbox = self.inbox
        a.RUNTIME_DIR = self.root
        a.bot = Mock()
        receipt = self.inbox.accept_dashboard('background-request', 'Make the report')
        a.send_message(OWNER_CHAT_ID, 'Background result', receipt['message_id'])
        self.assertEqual(conversation(self.root,OWNER_CHAT_ID)['messages'][-1]['text'],'Background result')
        a.bot.send_message.assert_not_called()

    def test_readiness_rejects_stale_pid_reuse_but_accepts_live_tool_deadline(self):
        from tabularium.channels.messaging import ready
        health={'pid':7,'messaging':1,'last_progress':time.time()-300,'deadline':time.time()+300}
        (self.root/'task_state/health.json').write_text(json.dumps(health))
        patcher=patch('tabularium.ops.supervisor.process_created',return_value=time.time()-1000)
        patcher.start()
        try:
            self.assertTrue(ready(self.root))
        finally:
            patcher.stop()

        patcher=patch('tabularium.ops.supervisor.process_created',return_value=time.time())
        patcher.start()
        try:
            self.assertFalse(ready(self.root))
        finally:
            patcher.stop()

    def test_worker_consumes_dashboard_message_through_registered_handler(self):
        import threading
        from tabularium.channels.messaging import conversation
        from tabularium.records.outbox import Outbox
        handled = threading.Event()
        outbox = Outbox(self.root, Mock(side_effect=AssertionError('No Telegram')))
        def handle_message(message):
            self.assertEqual(message.text, 'Actual queued input')
            self.assertEqual(self.inbox.destination(OWNER_CHAT_ID), 'dashboard')
            outbox.put(OWNER_CHAT_ID, ['Actual queued reply'], channel='dashboard')
            handled.set()
        self.inbox.handler(handle_message)
        receipt = self.inbox.accept_dashboard('queued-1', 'Actual queued input')
        self.inbox.start()
        try:
            self.assertTrue(handled.wait(8), 'Worker did not consume dashboard request')
        finally:
            self.inbox.close()
        rows = conversation(self.root,OWNER_CHAT_ID)['messages']
        self.assertEqual([r['text'] for r in rows], ['Actual queued input','Actual queued reply'])
        self.assertEqual(rows[0]['status'],'done')

    def test_http_authentication_and_idempotent_send(self):
        from tabularium.dashboard import server as dashboard
        import http.client
        import threading
        from http.server import ThreadingHTTPServer
        store = Mock()
        store.decisions.return_value=[]
        patches=[patch.object(dashboard,'ROOT',self.root),patch.object(dashboard,'task_store',return_value=store),
                 patch.object(dashboard.messaging,'ready',return_value=True)]
        for p in patches:
            p.start()
        server=ThreadingHTTPServer(('127.0.0.1',0),dashboard.Handler)
        runner=threading.Thread(target=server.serve_forever,daemon=True)
        runner.start()
        connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
        headers={'Host':f'127.0.0.1:{dashboard.PORT}','Origin':dashboard.URL,'Content-Type':'application/json'}
        body=json.dumps({'agent':'cato','client_id':'http-1','text':'HTTP request'})
        (self.root/'cato').mkdir()
        try:
            connection.request('POST','/api/messages',body,headers)
            response=connection.getresponse()
            self.assertEqual(response.status,403)
            response.read()
            self.assertFalse((self.root/'cato/task_state/inbox.sqlite3').exists())
            headers['X-CSRF-Token']=dashboard.TOKEN
            receipts=[]
            for _ in range(2):
                connection.request('POST','/api/messages',body,headers)
                response=connection.getresponse()
                self.assertEqual(response.status,200)
                receipts.append(json.loads(response.read()))
            self.assertEqual(receipts[0],receipts[1])
            connection.request('GET','/api/messages?agent=cato',headers={'Host':headers['Host']})
            response=connection.getresponse()
            self.assertEqual(response.status,200)
            self.assertEqual(len(json.loads(response.read())['messages']),1)
            headers['Origin']='https://untrusted.invalid'
            connection.request('POST','/api/messages',body,headers)
            response=connection.getresponse()
            self.assertEqual(response.status,403)
            response.read()
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            runner.join(5)
            for p in reversed(patches):
                p.stop()


if __name__ == '__main__':
    unittest.main()
