"""Independent evidence, isolation and real worker lifecycle for lesson screening."""
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
from agent_harness import put, make_agent, AGENTS
from agent_core import learning, trials, regression, session_lifecycle
from agent_core.outcome import Refused
import background_runtime as rt

SKILL = '---\nname: basket\nmetadata:\n  version: "1.8.5"\n---\n\nRead archives.\n'

def cases():
    return [dict(id=name, version=1, request=f'Save result.txt containing {value}.',
                 oracle=dict(basis=f'Explicit requested value {value}', reviewed_by='test author'),
                 requirements=[dict(id='value', check=dict(kind='text', path='result.txt', expected=value))])
            for name, value in [('dev', 'A'), ('held', 'B')]]


class Trials(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / 'cato'
        (self.root / 'task_state').mkdir(parents=True)
        db = sqlite3.connect(self.root / 'task_state/conversation_recall.sqlite3')
        try:
            db.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY, chat TEXT, speaker TEXT, text TEXT)')
            db.execute("INSERT INTO messages VALUES (7, '1', 'user', 'ok, approve it')")
            db.commit()
        finally:
            db.close()
        self.skill = self.root / 'skills/basket/SKILL.md'
        self.skill.parent.mkdir(parents=True)
        self.skill.write_text(SKILL, encoding='utf-8')
        self.key = learning.propose(self.root, 1, 'Freeze membership', 't1', 'basket_build')
        self.propose()
        self.store = rt.TaskStore(self.root)
        self.spec = dict(development_cases=['dev'], heldout_cases=['held'], repetitions=2, max_steps=12)
        self.corpus = cases()
        mock = patch.object(regression, 'load_cases', return_value=self.corpus)
        mock.start(); self.addCleanup(mock.stop)
        env = patch.dict(os.environ)
        env.start(); self.addCleanup(env.stop)
        for name in (trials.READS_ENV, trials.TRIAL_DIR_ENV, trials.OVERRIDES_ENV):
            os.environ.pop(name, None)

    def propose(self, text='Freeze membership.'):
        learning.propose_change(self.root, 1, self.key, self.skill, '', text)

    def start(self):
        return trials.start(self.root, 1, self.key, self.spec, self.store)

    def record(self):
        return learning.read(self.root, 1, self.key)['trial']

    def ctx(self, ident):
        return json.loads((self.store.directory(ident) / 'context.json').read_text(encoding='utf-8'))

    def settle(self, crash=False):
        for i, run in enumerate(self.record()['runs']):
            folder = self.store.directory(run['task'])
            out = folder / 'trial_output'
            out.mkdir(exist_ok=True)
            (out / 'result.txt').write_text('A' if run['case'] == 'dev' else 'B', encoding='utf-8')
            (folder / 'result.txt').write_text('Claim: everything passed.', encoding='utf-8')
            put(self.store, run['task'], 'interrupted' if crash and i == 0 else 'finished', 'Worker error: test')

    def test_reproposal_invalidates_completed_trial(self):
        learning.set_trial(self.root, 1, self.key, {'done': True})
        self.propose('A different rule.')
        self.assertNotIn('trial', learning.read(self.root, 1, self.key))
        self.assertRaisesRegex(ValueError, 'Run the trial first', learning.approve, self.root, 1, self.key, 7, 'ok, approve it')

    def test_candidate_is_exactly_the_applied_ledger_edit(self):
        p = learning.read(self.root, 1, self.key)['proposal']
        self.assertEqual(learning.render_change(SKILL, self.key, p), learning._bump_version(learning.add_entry(SKILL, self.key, p['new_text'], p['at'])))
        self.start(); self.settle()
        trials.reconcile(self.store, lambda *a: True)
        tested = Path(self.record()['folder'], 'variant.md').read_text(encoding='utf-8')
        learning.approve(self.root, 1, self.key, 7, 'ok, approve it')
        self.assertEqual(self.skill.read_text(encoding='utf-8'), tested)

    def test_trial_tools_cannot_escape_through_shell_or_search(self):
        runtime = NS(_all_tools=lambda p: [{'name': n} for n in ('read_file', 'write_file', 'execute_command', 'web_search', 'task_checkpoint')])
        trials.scope(runtime, {'trial': {'max_steps': 12}})
        self.assertEqual(runtime._tool_scope, {'read_file', 'write_file'})

    def test_freezes_both_arms_and_hides_oracles_before_launch(self):
        self.start()
        self.assertEqual(len(self.record()['runs']), 8)
        for run in self.record()['runs']:
            ctx = self.ctx(run['task'])
            self.assertFalse(ctx['notify_telegram'])
            self.assertNotIn('requirements', ctx)
            self.assertNotIn('oracle', ctx)
            self.assertTrue(Path(ctx['trial']['skill']).is_file())
        self.assertEqual(self.skill.read_text(encoding='utf-8'), SKILL)
        self.skill.write_text('Live changed', encoding='utf-8')
        baseline = Path(self.record()['folder'], 'baseline.md')
        self.assertEqual(baseline.read_text(encoding='utf-8'), SKILL)

    def test_inputs_must_be_disjoint_and_reviewed(self):
        self.spec['heldout_cases'] = ['dev']
        self.assertRaisesRegex(ValueError, 'disjoint', self.start)
        self.spec['heldout_cases'] = ['unreviewed']
        self.assertRaisesRegex(ValueError, 'reviewed', self.start)

    def test_exposed_case_cannot_be_relabelled_as_heldout(self):
        self.start(); self.settle()
        trials.reconcile(self.store, lambda *a: True)
        self.propose('Another candidate.')
        self.corpus[1]['id'] = 'renamed'
        self.spec['heldout_cases'] = ['renamed']
        self.assertRaisesRegex(ValueError, 'exposed', self.start)

    def test_crash_is_a_failed_attempt_not_silently_retried(self):
        self.start(); self.settle(crash=True)
        self.store.retry_interrupted()
        notices = []
        trials.reconcile(self.store, lambda c, t: notices.append(t))
        trials.reconcile(self.store, lambda c, t: notices.append(t))
        self.assertEqual(len(notices), 1)
        first = self.record()['outcomes'][0]
        self.assertFalse(first['grade']['passed'])
        self.assertEqual(first['status'], 'interrupted')
        self.assertIn('INCONCLUSIVE', notices[0])

    def test_not_graded_until_every_run_settles_and_delivery_retries(self):
        self.start()
        trials.reconcile(self.store, lambda *a: self.fail('Premature delivery'))
        self.assertFalse(self.record()['done'])
        self.settle()
        self.assertRaises(OSError, trials.reconcile, self.store, lambda *a: False)
        self.assertTrue(self.record()['done'])
        self.assertNotIn('notified', self.record())
        trials.reconcile(self.store, lambda *a: True)
        self.assertTrue(self.record()['notified'])

    def test_claims_do_not_replace_deliverables(self):
        self.start(); self.settle()
        run = self.record()['runs'][0]
        (self.store.directory(run['task']) / 'trial_output/result.txt').write_text('WRONG', encoding='utf-8')
        trials.reconcile(self.store, lambda *a: True)
        self.assertFalse(self.record()['outcomes'][0]['grade']['passed'])
        self.assertTrue(self.record()['outcomes'][1]['grade']['passed'])

    def test_reads_cannot_see_oracle_siblings_or_live_memory(self):
        self.start()
        run = self.record()['runs'][0]
        folder = self.store.directory(run['task'])
        trials.prepare_worker(self.ctx(run['task']), folder)
        self.assertIsNone(trials.read_refusal(self.ctx(run['task'])['trial']['skill']))
        self.assertIsNone(trials.read_refusal(folder / 'trial_output/x.txt'))
        for path in [self.root / 'memory.md', folder / 'context.json', Path(self.record()['folder']) / 'variant.md']:
            self.assertIsInstance(trials.read_refusal(path), Refused)
        self.assertIsInstance(trials.write_refusal(self.skill), Refused)

    def test_frozen_skill_tampering_stops_worker(self):
        self.start()
        ident = self.record()['runs'][0]['task']
        ctx = self.ctx(ident)
        Path(ctx['trial']['skill']).write_text('Changed', encoding='utf-8')
        self.assertRaisesRegex(ValueError, 'changed', trials.prepare_worker, ctx, self.store.directory(ident))

    def test_effort_limit_does_not_open_another_session(self):
        calls = []
        def run():
            calls.append(1)
            raise session_lifecycle.SessionLimit('limit')
        result = session_lifecycle.continue_turn(NS(_trial=True), [], 1, run)
        self.assertFalse(result.complete)
        self.assertEqual(calls, [1])

    def test_all_agents_enforce_trial_scope_and_skip_live_memory(self):
        for name in AGENTS:
            folder = self.root / name
            folder.mkdir()
            runtime = make_agent(name, folder, background=True)
            trials.scope(runtime, {'trial': {'max_steps': 4}})
            self.assertFalse(runtime._tool_scope - {'read_file', 'write_file'})
            for tool in ('execute_command', 'web_search', 'remember', 'task_decision', 'send_email'):
                self.assertIsInstance(runtime._execute_tool_unguarded(tool, {}, 1), Refused)
            runtime._load_core_memory = lambda: self.fail('Live memory accessed')
            window = [{'role': 'user', 'content': 'Frozen task'}]
            self.assertEqual(runtime._attach_current_context(window, 1), window)

    def test_real_workers_produce_artifacts_and_one_settled_report(self):
        (self.root / 'telegram.py').write_text(STUB, encoding='utf-8')
        self.spec['repetitions'] = 1
        self.start()
        notices = []
        self.store.last_monitor = time.time()
        try:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                self.store.tick(lambda c, t: notices.append(t))
                if self.record().get('notified'):
                    break
                time.sleep(.1)
            logs = '\n'.join((self.store.directory(r['task']) / 'worker.log').read_text(encoding='utf-8', errors='replace') for r in self.record()['runs'] if (self.store.directory(r['task']) / 'worker.log').exists())
            self.assertTrue(self.record().get('done'), logs)
            self.assertEqual(len(notices), 1, logs)
            self.assertTrue(all(r['grade']['passed'] for r in self.record()['outcomes']), logs)
            self.assertEqual(self.skill.read_text(encoding='utf-8'), SKILL)
        finally:
            for proc in self.store.processes.values():
                if proc.poll() is None:
                    proc.terminate()
                proc.wait(timeout=5)

    def test_standing_skill_is_replaced_not_duplicated(self):
        before = 'Follow this method:\nRead archives.\n'
        prompt = trials.skill_prompt(before, SKILL, SKILL.replace('Read archives.', 'Freeze membership.'))
        self.assertNotIn('Read archives.', prompt)
        self.assertEqual(prompt.count('Freeze membership.'), 1)

    def test_cannot_change_proposal_during_running_trial(self):
        self.start()
        self.assertRaisesRegex(ValueError, 'trial is running', self.propose, 'Another rule')

    def test_failed_scheduling_cannot_authorize_approval(self):
        mocked = patch.object(self.store, 'start', side_effect=OSError('queue unavailable'))
        mocked.start()
        try:
            self.assertRaises(OSError, self.start)
        finally:
            mocked.stop()
        trials.reconcile(self.store, lambda *a: True)
        self.assertFalse(trials.evidence_matches(learning.read(self.root, 1, self.key)))

    def test_comparison_requires_matching_routes_and_measured_tokens(self):
        self.start(); self.settle()
        trials.reconcile(self.store, lambda *a: True)
        r = self.record()
        for run in r['outcomes']:
            run['usage'] = dict(known=True, input_total=100, cache_read=0, output=20,
                                routes=[dict(provider='test', model='same', billing='test')])
        r['outcomes'][1]['usage']['input_total'] = 80
        report = trials.render_report(r)
        self.assertIn('input tokens -20', report)
        r['outcomes'][1]['usage']['routes'][0]['model'] = 'different'
        self.assertIn('INCONCLUSIVE', trials.render_report(r))
        self.assertNotIn('input tokens -20', trials.render_report(r))

    def test_pending_report_cannot_be_replaced_or_approved(self):
        self.start(); self.settle()
        self.assertRaises(OSError, trials.reconcile, self.store, lambda *a: False)
        self.assertRaisesRegex(ValueError, 'trial is running', self.propose)
        self.assertRaisesRegex(ValueError, 'Wait for the trial', learning.approve, self.root, 1, self.key, 7, 'ok, approve it', True)

    def test_legacy_finished_trial_needs_fresh_cases(self):
        learning.set_trial(self.root, 1, self.key, dict(done=True, baseline=0, variant=0))
        self.assertFalse(trials.evidence_matches(learning.read(self.root, 1, self.key)))
        self.start()
        self.assertEqual(self.record()['version'], 2)


STUB = '''from pathlib import Path
from types import SimpleNamespace as NS
import json, os
from background_runtime import TaskStore
AGENT_DIR = Path(__file__).resolve().parent
store = TaskStore(AGENT_DIR)
ident = int(os.environ["CATO_BACKGROUND_TASK"])
RUNTIME_DIR = store.directory(ident)
_inbox = NS(local=NS())
SYSTEM_PROMPT = "fixture"
conversations = {}
class RunawayStop(RuntimeError): pass
def load_model_prefs(chat): pass
def _conversation_path(chat): return RUNTIME_DIR / f"conversation_{chat}.json"
def load_conversation(chat): return []
def save_conversation(chat): _conversation_path(chat).write_text(json.dumps(conversations.get(chat, [])))
def run_turn(messages, chat):
    value = 'A' if 'containing A.' in messages[-1]['content'] else 'B'
    (RUNTIME_DIR / 'trial_output/result.txt').write_text(value, encoding='utf-8')
    return 'Artifact saved.'
runtime = NS(SYSTEM_PROMPT=SYSTEM_PROMPT, BACKGROUND_TASK_ID=str(ident), _tool_scope=None,
             _all_tools=lambda p: [{"name": "read_file"}, {"name": "write_file"}])
'''
