"""Review has to converge (2026-10-27, from the Claude Code team's code-review post, read with Alex).

Three reworks on one request are the cap; an item accepted as met cannot be sent back again; the review after a rework
records whether the delegate changed the artifact or waved the finding off; a report-only verdict needs a cold read of
that exact delivery first; an owner-tier request's acceptance records a decision for Alex, because Cato's accept is
never his approval; the digest says when the primary model moved on since the benchmark last ran."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from tests.agent_harness import make_agent, put
from tests.test_delegation_agreement import bind_delegate
from agent_core import verification, regression, specialist_review


class ReviewConvergence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'cato'
        self.root.mkdir()
        self.agent = make_agent('cato', self.root)
        self.store = self.agent._background
        self.task = self.store.start(1, 'Owner brief', completion_items=[dict(item='Synthesis', check='Read it', critical=True)])['id']
        self.items = [dict(item='Explain tradeoffs', check='Assess reasoning', critical=True, early=True),
                      dict(item='State the limits', check='Read them', critical=False)]
        self.remote, self.child = bind_delegate(self.store, self.task, 'request', self.items)
        self.store.record_submission('request', self.task, 'vit', 'engineering', 1000, self.items, brief=dict(question='Synthetic brief'))
        self.store.add_delegation(self.task, 'vit', 'request', 'engineering', 'worker_judgment')
        self.store.mark_delegation(self.task, 'request', 'returned')
        import delegation_contract
        p = patch.object(delegation_contract, 'BRIEFS', self.root / 'briefs')
        p.start()
        self.addCleanup(p.stop)
        self.remote_calls = []

    def runner(self, argv):                       # the bridge: a rework resumes the delegate, nothing else is checked here
        self.remote_calls.append(argv[3])
        return SimpleNamespace(returncode=0, stdout=json.dumps(dict(status='queued')), stderr='')

    def fetch(self):
        text = (self.remote.directory(self.child) / 'result.txt').read_text(encoding='utf-8')
        self.delivery_id = self.store.save_delegate_result(self.task, 'request', text)
        return self.delivery_id

    def accept(self, verdicts, how='judgment'):
        values = [dict(item=n, verdict=v, how=how, evidence='Read the delivered text against the brief in full.') for n, v in verdicts.items()]
        return json.loads(self.agent.tool_delegate(action='accept', chat_id=1, agent='vit', task_id=self.task, request_id='request',
                                                   delivery_id=self.delivery_id, verdicts=values,
                                                   brief_lesson='The brief should have named the missing tradeoff and asked for proof.'))

    def rework(self, items):
        return self.agent.tool_delegate(action='rework', chat_id=1, agent='vit', task_id=self.task, request_id='request',
                                        items=items, note='Name the gap', _runner=self.runner)

    def redeliver(self, text):
        put(self.remote, self.child, 'finished')
        (self.remote.directory(self.child) / 'result.txt').write_text(text, encoding='utf-8')
        self.fetch()

    def test_a_fourth_rework_is_refused_and_the_bridge_is_not_called(self):
        self.fetch()
        for n in range(verification.REWORK_CAP):
            self.accept({1: 'not_met', 2: 'met'})
            self.assertNotIn('cap', self.rework([1]))
            self.redeliver(f'DONE: attempt {n}')
        self.accept({1: 'not_met', 2: 'met'})
        refusal = self.rework([1])
        self.assertIn('the cap', refusal)
        self.assertIn('new request', refusal)
        self.assertEqual(self.remote_calls.count('rework'), verification.REWORK_CAP)
        self.assertEqual(self.store.scorecard('request')['reworks'], verification.REWORK_CAP)

    def test_an_item_accepted_as_met_cannot_be_sent_back_again(self):
        self.fetch()
        self.accept({1: 'not_met', 2: 'met'})
        refusal = self.rework([1, 2])
        self.assertIn('Items [2] were accepted as met', refusal)
        self.assertEqual(self.remote_calls, [])
        self.assertNotIn('accepted as met', self.rework([1]))

    def test_the_review_after_a_rework_records_a_change_or_a_wave_off(self):
        self.fetch()
        self.accept({1: 'not_met', 2: 'not_met'})
        self.rework([1, 2])
        log = json.loads(self.store.scorecard('request')['rework_log'])
        self.assertEqual((log[-1]['round'], log[-1]['items']), (1, [1, 2]))
        self.assertTrue(log[-1]['artifacts'])                           # the delivery's hashes as sent back
        self.redeliver('DONE: delivered result')                        # the delegate changed nothing
        self.accept({1: 'met', 2: 'met'})
        outcome = json.loads(self.store.scorecard('request')['rework_log'])[-1]['outcome']
        self.assertEqual(outcome, {'1': 'unchanged', '2': 'unchanged'})
        self.accept({1: 'not_met', 2: 'met'})
        self.rework([1])
        self.redeliver('DONE: a different result')
        self.accept({1: 'met', 2: 'met'})
        self.assertEqual(json.loads(self.store.scorecard('request')['rework_log'])[-1]['outcome'], {'1': 'changed'})
        from agent_core import scorecard
        week = scorecard.agent_week(self.root.parent, 'cato', time.time() - 60, time.time() + 60)['delegations']
        self.assertEqual((week['rework_changed'], week['rework_unchanged']), (1, 2))

    def test_a_report_only_verdict_needs_a_cold_read_of_that_delivery(self):
        self.fetch()
        out = self.agent.tool_delegate(action='accept', chat_id=1, agent='vit', task_id=self.task, request_id='request',
                                       delivery_id=self.delivery_id, brief_lesson='The brief should have asked for proof.',
                                       verdicts=[dict(item=n, verdict='met', how='report', evidence="The delegate's DONE line says it is there.") for n in (1, 2)])
        self.assertIn("rest on the delegate's word", out)
        self.assertIn(f"delivery_id='{self.delivery_id}'", out)
        folder = self.agent.AGENT_DIR / 'reports' / 'specialist_reviews'
        folder.mkdir(parents=True)
        (folder / '1.json').write_text(json.dumps(dict(role='delivery', delivery_id='another', status='finished')), encoding='utf-8')
        (folder / '2.json').write_text(json.dumps(dict(role='delivery', delivery_id=self.delivery_id, status='started')), encoding='utf-8')
        self.assertIsNone(verification.cold_review(self.agent.AGENT_DIR, self.delivery_id))
        (folder / '3.json').write_text(json.dumps(dict(role='delivery', delivery_id=self.delivery_id, status='finished')), encoding='utf-8')
        self.assertTrue(verification.cold_review(self.agent.AGENT_DIR, self.delivery_id))
        result = self.accept({1: 'met', 2: 'met'}, how='report')
        self.assertFalse(result['accepted'])                            # a cold read allows the review; report never passes
        self.assertEqual(result['critical_unmet'], [1])
        system, packet = specialist_review.prompt('Cato', 'delivery', 'Done means both items hold', 'E1: the result text')
        self.assertIn('no style findings', system)

    def test_an_owner_tier_acceptance_records_a_decision_for_owner(self):
        self.store.record_submission('owner-req', self.task, 'vit', 'engineering', 1000, self.items,
                                     brief=dict(question='Change the index engine', owner_tier='touches index calculation'))
        self.store.add_delegation(self.task, 'vit', 'owner-req', 'engineering', 'worker_judgment')
        self.remote2, self.child2 = bind_delegate(self.store, self.task, 'owner-req', self.items)
        self.store.mark_delegation(self.task, 'owner-req', 'returned')
        text = (self.remote2.directory(self.child2) / 'result.txt').read_text(encoding='utf-8')
        delivery_id = self.store.save_delegate_result(self.task, 'owner-req', text)
        out = json.loads(self.agent.tool_delegate(action='accept', chat_id=1, agent='vit', task_id=self.task, request_id='owner-req',
                                                  delivery_id=delivery_id,
                                                  verdicts=[dict(item=n, verdict='met', how='judgment', evidence='Read the delivered text against the brief in full.') for n in (1, 2)]))
        self.assertTrue(out['accepted'])
        self.assertIn('Owner tier: decision #', out['next'])
        pending = self.store.decisions(1, self.task)
        self.assertEqual(len(pending), 1)
        self.assertIn('touches index calculation', pending[0]['question'])
        self.assertIn('only on your yes', pending[0]['question'])
        self.assertEqual(self.store.get(self.task)['status'], 'needs_answer')

    def test_the_digest_says_when_the_model_moved_on_since_the_last_benchmark_run(self):
        out = self.root / 'task_state' / 'regression'
        out.mkdir(parents=True, exist_ok=True)
        with patch.object(regression, 'primary_route', return_value='claude-opus-5-5'):
            self.assertIsNone(regression.route_gap(self.root))          # no run yet: nothing to compare
            (out / '20261025-030125.json').write_text(json.dumps(dict(route='claude-opus-5-5', pass_k=1)), encoding='utf-8')
            self.assertIsNone(regression.route_gap(self.root))
        with patch.object(regression, 'primary_route', return_value='gpt-6-luna'):
            line = regression.route_gap(self.root)
        self.assertIn('BENCHMARK? the primary model is now gpt-6-luna', line)
        self.assertIn('20261025-030125', line)
        self.assertIn('eval cato', line)


if __name__ == '__main__':
    unittest.main()
