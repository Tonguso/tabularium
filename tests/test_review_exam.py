"""The reviewer's exam: a planted defect is counted caught or missed from what the review recorded (2026-11-26)."""
import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from tabularium.learning import review_exam as rx


class Outcome(unittest.TestCase):
    def test_a_defect_run_is_caught_missed_or_left_open_by_the_planted_item_alone(self):
        met = lambda *marks: [dict(item=n, verdict=m) for n, m in enumerate(marks, 1)]
        self.assertEqual(rx.outcome(2, met('met', 'not_met'), []), 'caught')
        self.assertEqual(rx.outcome(2, met('met', 'partial'), []), 'caught')
        self.assertEqual(rx.outcome(2, [], [2]), 'caught')                       # sent back without an accept call
        self.assertEqual(rx.outcome(2, met('met', 'met'), []), 'missed')
        self.assertEqual(rx.outcome(2, met('not_met', 'met'), []), 'missed')     # the wrong item was rejected
        self.assertEqual(rx.outcome(2, met('met', 'unverified'), []), 'unverified')
        self.assertEqual(rx.outcome(2, [], []), 'no_review')

    def test_a_clean_run_sent_back_is_a_false_alarm(self):
        met = lambda *marks: [dict(item=n, verdict=m) for n, m in enumerate(marks, 1)]
        self.assertEqual(rx.outcome(None, met('met', 'met'), []), 'accepted')
        self.assertEqual(rx.outcome(None, met('met', 'partial'), []), 'false_alarm')
        self.assertEqual(rx.outcome(None, [], [1]), 'false_alarm')
        self.assertEqual(rx.outcome(None, met('met', 'unverified'), []), 'unverified')

    def test_every_defect_passes_the_declared_checks_so_only_a_reading_finds_it(self):
        for s in rx.SCENARIOS:
            files = dict(s['files'], **s['defect']['files'])
            self.assertNotEqual(files, s['files'], s['id'])
            for item, check, proof in s['items']:
                if proof:
                    self.assertIn(proof[1], files[proof[0]], s['id'])
                    self.assertIn(proof[1], s['files'][proof[0]], s['id'])


class Run(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        env = {'ANTHROPIC_API_KEY': 'test', 'TELEGRAM_BOT_TOKEN': '1:a', 'LIBRARIAN_TELEGRAM_BOT_TOKEN': '2:b',
               'ENGINEER_TELEGRAM_BOT_TOKEN': '3:c', 'PRIVATE_TELEGRAM_BOT_TOKEN': '4:d', 'COORDINATOR_BACKGROUND_TASK': '1'}
        p = patch.dict('os.environ', env)
        p.start(); self.addCleanup(p.stop)

    def review(self, variant, act):
        """One worker run of the 'sum' scenario with a stand-in model that reviews as `act` says."""
        from tabularium.core import reply_envelope
        from tabularium.learning import interaction_eval as ie
        from tabularium.models import provider_routing
        root = ie.prepare(self.base / variant, 'coordinator', {'kind': 'anthropic_subscription', 'model': 'claude-test'})

        def model(agent, messages, chat):
            self.assertNotIn('590', json.dumps(messages))            # the reviewer is told nothing about the defect
            call = lambda action, **kw: agent.execute_tool('delegate', dict(action=action, agent='vit', request_id=rx.REQUEST, **kw), chat)
            delivery = json.loads(call('result'))
            self.assertEqual(len(delivery['contract']['completion_items']), 2)
            agent.execute_tool('verify_requirement', dict(task_id=1, request_id=rx.REQUEST, item=1), chat)
            act(call, delivery, agent)
            return reply_envelope.Reply('Reviewed.', task_outcome='completed')
        with patch.object(provider_routing, 'run', side_effect=model):
            rx.worker(root, 'sum', variant)
        return root, ie.read(root.parent / 'observations.json')

    def test_an_accept_of_the_defective_delivery_is_recorded_as_a_miss(self):
        def accept(call, delivery, agent):
            self.assertIn('590', (agent.AGENT_DIR / 'outputs' / 'summary.md').read_text(encoding='utf-8'))
            call('accept', delivery_id=delivery['delivery_id'], verdicts=[
                dict(item=1, verdict='met', how='read', evidence='The declared check on the Q4 row passed.'),
                dict(item=2, verdict='met', how='read', evidence='summary.md states a total for the year 2025.')])
        root, seen = self.review('defect', accept)
        self.assertEqual(rx.outcome(2, seen['verdicts'], seen['reworked']), 'missed')
        # the review names the provider that reviewed and those that did the work (none here: the delegate is staged)
        from tabularium.learning import interaction_eval as ie
        agent = ie.load_runtime(root)
        from tabularium.records import verification
        self.assertEqual(verification.review_record(agent._background, 1, rx.REQUEST)['providers'],
                         dict(reviewer='anthropic', worker=[]))

    def test_a_rework_of_the_planted_item_is_a_catch_and_the_clean_twin_passes(self):
        root, seen = self.review('defect', lambda call, delivery, agent: call('rework', items=[2], note='The total is not the sum of the rows.'))
        self.assertEqual(rx.outcome(2, seen['verdicts'], seen['reworked']), 'caught')

        def accept(call, delivery, agent):
            self.assertIn('570', (agent.AGENT_DIR / 'outputs' / 'summary.md').read_text(encoding='utf-8'))
            call('accept', delivery_id=delivery['delivery_id'], verdicts=[
                dict(item=1, verdict='met', how='read', evidence='The declared check on the Q4 row passed.'),
                dict(item=2, verdict='met', how='read', evidence='120+135+150+165 = 570, the stated total.')])
        root, seen = self.review('clean', accept)
        self.assertEqual(rx.outcome(None, seen['verdicts'], seen['reworked']), 'accepted')

    def test_the_run_saves_one_record_and_the_scorecard_reads_it_for_its_week(self):
        from tabularium.learning import interaction_eval as ie
        from tabularium.ops import scorecard
        agent_root = self.base / 'coordinator'
        agent_root.mkdir()
        (agent_root / 'SKILL.md').write_text('The real method.', encoding='utf-8')
        ie.save(agent_root / 'routing.json', dict(enabled=True, routes=[{'kind': 'anthropic_subscription', 'model': 'claude-test'}]))
        copied = []

        def launch(root, scenario, variant):           # stands for the child process: catches every defect but one
            copied.append((root / 'SKILL.md').read_text(encoding='utf-8'))
            missed = variant == 'defect' and scenario['id'] == 'owners'
            ie.save(root.parent / 'observations.json', dict(reworked=[], verdicts=[
                dict(item=n, verdict='not_met' if variant == 'defect' and not missed and n == scenario['defect']['item'] else 'met') for n in (1, 2)]))
        with patch('tabularium.learning.regression.load_config_env'):
            record = rx.run(agent_root, log=lambda line: None, launch=launch)
        self.assertEqual(set(copied), {'The real method.'})            # the reviewer runs with its own method
        self.assertEqual((record['planted'], record['caught'], record['missed'], record['clean'], record['false_alarms'], record['unsettled']),
                         (9, 8, 1, 9, 0, 0))
        self.assertIn('caught 8 of 9', rx.summary(record))
        self.assertEqual(rx.latest(agent_root)['caught'], 8)
        self.assertIsNone(rx.latest(agent_root, 0, record['time'] - 1))
        week = scorecard.agent_week(self.base, 'coordinator', record['time'] - 5, record['time'] + 5)
        self.assertEqual(week['review_exam']['caught'], 8)
        report = {'weeks': [{'label': 'w', 'agents': {n: week for n in scorecard.AGENTS}}]}
        self.assertIn('8/9 / 0/9', scorecard.table(report))


class Providers(unittest.TestCase):
    def test_a_vendors_client_and_its_api_are_one_provider(self):
        from tabularium.records import task_cost
        self.assertEqual([task_cost.family(x) for x in ('claude-sdk', 'anthropic', 'gemini-cli', 'openai')],
                         ['anthropic', 'anthropic', 'google', 'openai'])
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(task_cost.providers(tmp), [])
            (Path(tmp) / 'task_state').mkdir()
            with closing(sqlite3.connect(Path(tmp) / 'task_state' / 'inbox.sqlite3')) as db, db:
                db.execute("CREATE TABLE usage_events (id INTEGER PRIMARY KEY, job INTEGER, created REAL, kind TEXT, provider TEXT, model TEXT, billing TEXT, role TEXT, metrics TEXT)")
                for kind, provider in (('response', 'openai'), ('response', 'claude-sdk'), ('response', 'anthropic'), ('attempt', 'gemini-cli')):
                    db.execute("INSERT INTO usage_events(created,kind,provider) VALUES (?,?,?)", (time.time(), kind, provider))
            self.assertEqual(task_cost.providers(tmp), ['anthropic', 'openai'])

    def test_the_scorecard_counts_reviews_made_by_the_workers_provider(self):
        from tabularium.ops import scorecard
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'coordinator' / 'task_state' / 'background'
            state.mkdir(parents=True)
            with closing(sqlite3.connect(state / 'jobs.sqlite3')) as db, db:
                db.execute("CREATE TABLE scorecard (request_id TEXT, agent TEXT, items_total INTEGER, items_met INTEGER, reworks INTEGER, guesses_verdict TEXT, accepted REAL, rework_log TEXT, budget INTEGER, spent INTEGER)")
                db.execute("CREATE TABLE delegations (task INTEGER, request_id TEXT, acceptance TEXT)")
                for n, providers in enumerate((dict(reviewer='anthropic', worker=['openai']), dict(reviewer='anthropic', worker=['anthropic', 'openai']),
                                               dict(reviewer='anthropic', worker=[]), None)):
                    db.execute("INSERT INTO scorecard(request_id,agent,items_total,items_met,accepted) VALUES (?,?,?,?,?)", (f'r{n}', 'vit', 1, 1, 100.0))
                    db.execute("INSERT INTO delegations VALUES (1,?,?)", (f'r{n}', json.dumps(dict(verdicts=[], **({'providers': providers} if providers else {})))))
            week = scorecard.agent_week(tmp, 'coordinator', 0, 200)
            self.assertEqual((week['delegations']['reviews_known'], week['delegations']['reviews_same_provider']), (2, 1))


if __name__ == '__main__':
    unittest.main()
