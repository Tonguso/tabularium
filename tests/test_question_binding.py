"""A reply answers its own question; optional proposals cannot reopen delivered work."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from tests.agent_harness import make_agent, put
from tabularium.records import task_decisions as td
from tabularium.core.reply_envelope import Reply, reply
from tabularium.core.roster import AGENTS


class QuestionBinding(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def agent(self, name='cato'):
        p = self.root / name
        p.mkdir(exist_ok=True)
        return make_agent(name, p)

    def test_new_conversation_question_never_reuses_unrelated_pending_question(self):
        for name in AGENTS:
            a = self.agent(name)
            first = a._background.ask(1, 0, 'Draw near-threshold baskets?')
            r = td.verify_reply(a, [], 1, Reply('Which font?', question='Which font?'))
            self.assertNotEqual(first['id'], r.decision_id)
            td.bind_messages(a._background, r.decision_id, [900])
            self.assertEqual(td.by_message(a._background, 1, 900)['question'], 'Which font?')
            self.assertEqual(len(a._background.decisions(1)), 2)

    def test_reply_is_a_candidate_not_an_answer_or_continuation(self):
        for name in AGENTS:
            a = self.agent(name)
            task = a._background.start(1, 'Make a report')['id']
            d = a._background.ask(1, task, 'Choose A or B')
            td.bind_messages(a._background, d['id'], [900])
            for text in ('Why?', 'Use Lato', 'Choose B'):
                msg = SimpleNamespace(chat=SimpleNamespace(id=1), message_id=901,
                    text=text, reply_to_message=SimpleNamespace(message_id=900))
                handled, note = a._settle_by_reply(msg)
                self.assertFalse(handled)
                self.assertIn('task_decision', note)
                self.assertEqual(a._background.decisions(1, task)[0]['state'], 'pending')
                self.assertEqual(a._background.pending_controls(task), [])

    def test_explicit_id_survives_provider_envelope(self):
        r = reply(dict(reply='Choose B?', question='Choose B?', decision_id=42))
        self.assertEqual(r.decision_id, 42)

    def test_completed_worker_keeps_optional_question_without_blocking_delivery(self):
        a = self.agent()
        s = a._background
        put(s, 1, 'finished')
        db = sqlite3.connect(s.database)
        try:
            db.execute('UPDATE tasks SET notified=1 WHERE id=1')
            db.commit()
        finally:
            db.close()
        task = s.start(1, 'Research the options', completion_items=[dict(item='Report', check='Read it', critical=True)])['id']
        put(s, task, 'running')
        r = Reply('Research delivered. Start a separate trial?', task_outcome='completed', question='Start a separate trial?')
        self.assertEqual(s.finish(task, r), 'finished')
        self.assertEqual(s.decisions(1, task), [])
        d = s.decisions(1, 0)[0]
        self.assertEqual(d['question'], 'Start a separate trial?')
        record = json.loads((s.directory(task) / 'completion.json').read_text())
        self.assertEqual(record['decision_id'], d['id'])
        (s.directory(task) / 'result.txt').write_text(str(r), encoding='utf-8')
        notices = []
        s.tick(lambda chat, text: notices.append(text))
        self.assertEqual(notices[0].decision_id, d['id'])
        td.answer_record(s, 1, d['id'], 'Yes', 'synthetic verified source')
        self.assertEqual(s.get(task)['status'], 'finished')
        self.assertEqual(s.pending_controls(task), [])

    def test_explicit_id_cannot_relabel_a_different_question(self):
        a = self.agent()
        d = a._background.ask(1, 0, 'Draw baskets?')
        r = Reply('Which font?', question='Which font?')
        r.decision_id = d['id']
        out = td.verify_reply(a, [], 1, r)                    # the wrong id is dropped, never a crash (2026-10-27 23:27)
        self.assertNotEqual(out.decision_id, d['id'])
        self.assertEqual({x['question']: x['id'] for x in a._background.decisions(1)},
                         {'Draw baskets?': d['id'], 'Which font?': out.decision_id})

    def test_verified_current_answer_resumes_once_and_preserves_other_decisions(self):
        a = self.agent()
        s = a._background
        task = s.start(1, 'Build report')['id']
        d = s.ask(1, task, 'Choose A or B')
        other = s.ask(1, 0, 'Draw baskets?')
        db = sqlite3.connect(a._inbox.database)
        try:
            db.execute("INSERT INTO jobs(chat,message,handler,payload,status,created) VALUES (1,901,'handle_message',?,'running',0)",
                       (json.dumps({'text': 'Choose B'}),))
            db.commit()
        finally:
            db.close()
        a._inbox.local.message_id = 901
        bad = td.execute(a, 1, dict(action='answer', decision_id=d['id'], answer='A', quote='Choose A'))
        self.assertIn('not in', str(bad))
        self.assertEqual(s.pending_controls(task), [])
        values = dict(action='answer', decision_id=d['id'], answer='B', quote='Choose B')
        first = td.execute(a, 1, values)
        self.assertEqual(td.execute(a, 1, values), first)
        self.assertEqual(s.get(task)['status'], 'queued')
        self.assertEqual(len(s.pending_controls(task)), 1)
        self.assertEqual([x['id'] for x in s.decisions(1)], [other['id']])

    def test_stale_worker_cannot_create_an_optional_question(self):
        a = self.agent()
        s = a._background
        s.worker_attempt = 'obsolete'
        self.assertRaises(ValueError, s.finish, 1,
            Reply('Done. Another project?', task_outcome='completed', question='Another project?'))
        self.assertEqual(s.decisions(1), [])

    def test_optional_question_does_not_override_a_real_blocker(self):
        a = self.agent()
        s = a._background
        task = s.start(1, 'Report', completion_items=[dict(item='Report', check='Read it', critical=True)])['id']
        put(s, task, 'running')
        (s.directory(task) / 'checkpoint.json').write_text(json.dumps({'blocker': 'Missing required data'}))
        self.assertEqual(s.finish(task, Reply('Partial. Another report?', task_outcome='completed',
                                             question='Another report?')), 'needs_attention')
        self.assertEqual(s.decisions(1, task), [])
        self.assertEqual(len(s.decisions(1, 0)), 1)

    def test_questionless_reply_cannot_carry_a_decision_binding(self):
        a = self.agent()
        d = a._background.ask(1, 0, 'Draw baskets?')
        r = Reply('Here is the weather.', decision_id=d['id'])
        self.assertEqual(td.verify_reply(a, [], 1, r).decision_id, 0)
        self.assertEqual([x['id'] for x in a._background.decisions(1)], [d['id']])

    def test_action_id_cannot_bind_an_unrelated_question(self):
        a = self.agent()
        d = a._background.ask(1, 0, 'Send the report?')
        td.park_action(a._background, 1, 'send_email', {'to': 'test@example.test'}, 'Send?', d['id'])
        r = Reply('Which font?', question='Which font?', decision_id=d['id'])
        out = td.verify_reply(a, [], 1, r)
        self.assertNotEqual(out.decision_id, d['id'])
        self.assertEqual(next(x for x in a._background.decisions(1) if x['id'] == d['id'])['kind'], 'action')
