"""Offline regression checks. No live tokens, messages, models, or process control."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
from tabularium.core.agent_runtime import DurableInbox, atomic_json, recover_tool_history
from tabularium.channels.owner_telegram import OwnerBot, is_owner, OWNER_CHAT_ID
from telebot.types import Message


def message(i, owner=OWNER_CHAT_ID, kind='private', album=None):
    raw = {'message_id': i, 'date': 1700000000, 'chat': {'id': OWNER_CHAT_ID, 'type': kind},
           'from': {'id': owner, 'is_bot': False, 'first_name': 'Owner'}, 'text': f'request {i}'}
    if album:
        raw.update(media_group_id=album, photo=[{'file_id': str(i), 'file_unique_id': str(i), 'width': 10, 'height': 10}])
    return Message.de_json(raw)


def wait_for(predicate):
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError('Timed out waiting for isolated worker')


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + '.py'))
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


class Reliability(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sent = []
        self.inbox = DurableInbox(self.root, lambda *args: self.sent.append(args))

    def tearDown(self):
        self.inbox.close()
        self.temp.cleanup()

    def statuses(self):
        with self.inbox.db() as db:
            return [row[0] for row in db.execute('SELECT status FROM jobs ORDER BY id')]

    def test_owner_gate_before_dispatch(self):
        bot = OwnerBot.__new__(OwnerBot)
        bot.last_update_id = 0
        allowed, denied, group = message(1), message(2, owner=999), message(3, kind='group')
        updates = [SimpleNamespace(update_id=i, message=m) for i, m in enumerate([allowed, denied, group], 1)]
        with patch('telebot.TeleBot.process_new_updates') as dispatch:
            bot.process_new_updates(updates)
        self.assertEqual(dispatch.call_args.args[0], updates[:1])
        self.assertEqual(bot.last_update_id, 3)
        self.assertFalse(is_owner(denied))

    def test_busy_queue_preserves_order_and_deduplicates(self):
        began, release = threading.Event(), threading.Event()
        calls = []
        @self.inbox.handler
        def consume(m):
            calls.append(m.message_id)
            if m.message_id == 1:
                began.set()
                self.assertTrue(release.wait(5))
        consume(message(1))
        self.inbox.start()
        self.assertTrue(began.wait(3))
        consume(message(2)); consume(message(2)); consume(message(3, owner=999))
        self.assertEqual(self.statuses(), ['running', 'pending'])
        release.set()
        wait_for(lambda: self.statuses() == ['done', 'done'])
        self.assertEqual(calls, [1, 2])

    def test_cancel_queued_work(self):
        calls = []
        @self.inbox.handler
        def consume(m):
            calls.append(m.message_id)
        consume(message(1))
        self.assertEqual(self.inbox.cancel(OWNER_CHAT_ID), 1)
        self.inbox.start()
        self.assertEqual(self.statuses(), ['cancelled'])
        self.assertEqual(calls, [])

    def test_album_keeps_both_images(self):
        calls = []
        @self.inbox.handler(album=True)
        def photos(batch):
            calls.append([m.message_id for m in batch])
        photos(message(1, album='a')); photos(message(2, album='a'))
        self.inbox.start()
        wait_for(lambda: self.statuses() == ['done', 'done'])
        self.assertEqual(calls, [[1, 2]])

    def test_an_album_behind_other_work_gets_one_queue_notice(self):
        """2026-11-02: seven photos sent as one album while Cato was busy drew a 'queued behind them' notice each."""
        @self.inbox.handler(album=True)
        def photos(batch):
            pass
        @self.inbox.handler
        def consume(m):
            pass
        consume(message(1)); consume(message(2))                 # one running, one waiting: the next request is third in line
        for i in range(3, 8):
            photos(message(i, album='a'))
        notices = [s for s in self.sent if 'queued behind' in s[1]]
        self.assertEqual(len(notices), 1)

    def test_restart_holds_interrupted_actions(self):
        calls = []
        @self.inbox.handler
        def consume(m):
            calls.append(m.message_id)
        consume(message(1)); consume(message(2))
        with self.inbox.db() as db:
            db.execute("UPDATE jobs SET status='running' WHERE message=1")
        self.inbox.start()
        self.assertEqual(self.statuses(), ['interrupted', 'held'])
        self.assertEqual(calls, [])
        self.assertIn('original Telegram request', self.inbox.recovery_note(OWNER_CHAT_ID))

    def test_malformed_job_fails_once(self):
        with self.inbox.db() as db:
            db.execute("INSERT INTO jobs(chat,message,handler,payload,status,created) VALUES (1,1,'missing','{}','pending',0)")
        self.inbox.start()
        wait_for(lambda: self.statuses() == ['failed'])
        self.assertEqual(self.sent, [])  # never notify an unauthorized legacy chat

    def test_atomic_write_preserves_previous_on_failure(self):
        path = self.root / 'state.json'
        atomic_json(path, {'old': True})
        with patch('tabularium.core.agent_runtime.os.replace', side_effect=OSError('simulated disk error')):
            with self.assertRaises(OSError):
                atomic_json(path, {'new': True})
        self.assertEqual(json.loads(path.read_text()), {'old': True})

    def test_completed_action_recovered_without_replay_or_mutation(self):
        self.inbox.local.tool_id = 'call1'
        calls = []
        @self.inbox.tool
        def action(name, inputs, chat_id):
            calls.append(name)
            return 'sent already'
        action('send_email', {}, OWNER_CHAT_ID)
        history = [{'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'call1', 'name': 'send_email', 'input': {}}, {'type': 'tool_use', 'id': 'call2', 'name': 'read_file', 'input': {}}]}, {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call2', 'content': 'read'}]}]
        original = copy.deepcopy(history)
        recovered = recover_tool_history(history, self.root)
        self.assertEqual(history, original)
        self.assertEqual(recovered[1]['content'][0]['content'], 'sent already')
        self.assertEqual(calls, ['send_email'])

    def test_unknown_action_is_not_assumed_failed(self):
        history = [{'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'call', 'name': 'send_email', 'input': {}}]}]
        self.assertIn('UNKNOWN', recover_tool_history(history, self.root)[1]['content'][0]['content'])

    def test_usage_keeps_routes_and_token_categories_separate(self):
        @self.inbox.handler
        def consume(m):
            pass
        consume(message(1))
        self.inbox.local.job = 1
        self.inbox.record_event('attempt', 'sol', 'subscription', 'openai')
        self.inbox.record_event('attempt', 'sol', 'api', 'openai')
        self.inbox.record_event('response', 'sol', 'api', 'openai',
                                {'input_tokens': 1000, 'input_tokens_details': {'cached_tokens': 800, 'cache_write_tokens': 0}, 'output_tokens': 50})
        self.inbox.record_event('response', 'opus', 'api', 'anthropic',
                                {'input_tokens': 100, 'cache_read_input_tokens': 400, 'cache_creation_input_tokens': 20, 'output_tokens': 30}, role='advisor')
        summary = self.inbox.usage_summary(OWNER_CHAT_ID)
        self.assertIn('input_uncached=200', summary)
        self.assertIn('cache_read=800', summary)
        self.assertIn('input_uncached=100', summary)
        self.assertIn('cache_write=20', summary)
        self.assertIn('subscription | worker: 1 application attempts, 0 responses', summary)
        self.assertIn('opus | api | advisor', summary)
        self.assertNotIn('request 1', summary)

    def test_missing_usage_is_not_reported_as_zero(self):
        @self.inbox.handler
        def consume(m):
            pass
        consume(message(1))
        self.inbox.local.job = 1
        self.inbox.record_event('response', 'sol', 'api', 'openai', None)
        self.assertIn('Token usage unavailable', self.inbox.usage_summary(OWNER_CHAT_ID))

    def test_usage_logging_failure_does_not_abort_task(self):
        with patch.object(self.inbox, 'db', side_effect=OSError('disk full')):
            self.inbox.record_event('response', 'sol', 'api', 'openai', None)


if __name__ == '__main__':
    unittest.main(verbosity=2)
