"""Completion is derived from evidence, not a completed flag or a sentinel in prose."""
import json
from pathlib import Path
import tempfile
import unittest
from tests.agent_harness import make_agent, put
from tabularium.core.roster import AGENTS
from tabularium.core.reply_envelope import Reply


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def agent(self, name='cato'):
        root = self.root / name
        root.mkdir(exist_ok=True)
        agent = make_agent(name, root, background=True)
        artifact = root / 'result.txt'
        artifact.write_text('correct', encoding='utf-8')
        agent._background.checkpoint(1, 1, '', '', [], guesses=[], completion_items=[{
            'item': 'Correct result', 'check': 'Read actual artifact',
            'verification': {'kind': 'contains', 'paths': [str(artifact)], 'text': 'correct'}}])
        return agent, artifact

    def test_missing_failed_and_stale_proof_never_finish_for_all_roles(self):
        for name in AGENTS:
            agent, artifact = self.agent(name)
            store = agent._background
            result = Reply('Everything is done', task_outcome='completed')
            self.assertEqual(store.finish(1, result), 'needs_attention', name)
            put(store, 1, 'running')
            artifact.write_text('wrong', encoding='utf-8')
            agent._execute_tool_unguarded('verify_requirement', {'task_id': 1, 'item': 1}, 1)
            self.assertEqual(store.finish(1, result), 'needs_attention', name)
            put(store, 1, 'running')
            artifact.write_text('correct', encoding='utf-8')
            agent._execute_tool_unguarded('verify_requirement', {'task_id': 1, 'item': 1}, 1)
            artifact.write_text('changed after check', encoding='utf-8')
            self.assertEqual(store.finish(1, result), 'needs_attention', name)
            put(store, 1, 'running')
            artifact.write_text('correct', encoding='utf-8')
            agent._execute_tool_unguarded('verify_requirement', {'task_id': 1, 'item': 1}, 1)
            self.assertEqual(store.finish(1, result), 'finished', name)

    def test_judgment_requirements_finish_and_add_nothing_to_the_message(self):
        """2026-10-24, Alex: [quote removed] (a research result delivered under "Completion not
        verified: Requirement 1 Direct comparator table ...: unverified."). Requirements no machine can check are the
        owner's to judge by reading; they never block finishing and never reach the message. A declared check still
        has to pass, and only a failed or unrun declared check is named, in plain words."""
        from tabularium.records import verification
        for name in AGENTS:
            root = self.root / ("judge-" + name); root.mkdir(exist_ok=True)
            agent = make_agent(name, root, background=True)
            store = agent._background
            store.checkpoint(1, 1, '', '', [], guesses=[], completion_items=[
                {'item': 'Direct comparator table', 'check': 'Owner reads it'},
                {'item': 'Conclusion and README pitch', 'check': 'Owner reads it'}])
            self.assertEqual(store.finish(1, Reply('Report attached.', task_outcome='completed')), 'finished', name)
            self.assertEqual(verification.summary(store, 1), '', name)
            self.assertEqual(verification.summary(store, 1), '', name)
            # mixed: one judgment item and one declared check that was never run
            put(store, 1, 'running')
            artifact = root / 'report.md'; artifact.write_text('table', encoding='utf-8')
            store.checkpoint(1, 1, '', '', [], guesses=[], completion_items=[
                {'id': 3, 'item': 'Report saved', 'check': 'File exists with the table',
                 'verification': {'kind': 'contains', 'paths': [str(artifact)], 'text': 'table'}}])
            self.assertEqual(store.finish(1, Reply('Report attached.', task_outcome='completed')), 'needs_attention', name)
            self.assertEqual(verification.summary(store, 1), 'Not checked on the delivered file: Report saved.', name)
            put(store, 1, 'running')
            agent._execute_tool_unguarded('verify_requirement', {'task_id': 1, 'item': 3}, 1)
            self.assertEqual(store.finish(1, Reply('Report attached.', task_outcome='completed')), 'finished', name)
            self.assertEqual(verification.summary(store, 1), '', name)

    def test_prose_cannot_bypass_unaccepted_delegation(self):
        agent, _ = self.agent()
        store = agent._background
        store.add_delegation(1, 'vit', 'pending-proof', 'Check result', 'worker_judgment')
        store.mark_delegation(1, 'pending-proof', 'returned')
        self.assertIsNotNone(store.delivery_block(1, 'NOT DONE: skip this dependency'))
        self.assertEqual(store.finish(1, Reply('NOT DONE: skip this dependency', task_outcome='completed')), 'needs_attention')

    def test_missing_requirements_and_invalid_checkpoint_fail_closed(self):
        agent, _ = self.agent()
        store = agent._background
        p = store.directory(1) / 'checkpoint.json'
        p.unlink()
        self.assertEqual(store.finish(1, Reply('Done', task_outcome='completed')), 'needs_attention')
        put(store, 1, 'running')
        p.write_text('{broken', encoding='utf-8')
        self.assertEqual(store.finish(1, Reply('Done', task_outcome='completed')), 'needs_attention')

    def test_an_added_instruction_without_a_declared_requirement_does_not_block_finishing(self):
        """2026-10-24, Alex: [quote removed] (task 348 delivered under "Not complete: Instructions without
        declared requirements: [235]"; 235 was the runtime's own resume note). Whether an instruction carries a
        declared requirement is guidance to the worker, never evidence against finished work. Earlier proof stays
        valid, and no reason about it reaches completion.json."""
        agent, _ = self.agent()
        store = agent._background
        agent._execute_tool_unguarded('verify_requirement', {'task_id': 1, 'item': 1}, 1)
        store.control(1, 1, 'steer', 'Continue the read-through from the saved checkpoint; same constraints as the brief.')
        store.acknowledge([x['id'] for x in store.pending_controls(1)])
        self.assertEqual(store.finish(1, Reply('Done', task_outcome='completed')), 'finished')
        decision = json.loads((store.directory(1) / 'completion.json').read_text())
        self.assertTrue(decision['passed'])
        self.assertNotIn('without declared requirements', ' '.join(decision['reasons']))

    def test_inflight_dependency_parks_without_model_calls_and_wakes_on_return(self):
        agent, _ = self.agent()
        store = agent._background
        store.add_delegation(1, 'vit', 'pending-work', 'Check result', 'worker_judgment')
        store.status_checker = lambda *args: 'running'
        self.assertEqual(store.finish(1, Reply('Waiting', task_outcome='waiting')), 'waiting_on_delegate')
        self.assertFalse(json.loads((store.directory(1) / 'completion.json').read_text())['passed'])
        store.status_checker = lambda *args: 'completed'
        store.poll_waiting()
        self.assertEqual(store.get(1)['status'], 'queued')

    def test_eval_generation_is_recorded_but_missing_artifact_is_not_success(self):
        agent, _ = self.agent()
        store = agent._background
        from tabularium.learning import regression
        case = regression.load_cases(self.root)[0]
        context = {'eval': {'case': case['id'], 'benchmark': regression.fingerprint([case])}, 'notify_telegram': False}
        (store.directory(1) / 'context.json').write_text(json.dumps(context), encoding='utf-8')
        self.assertEqual(store.finish(1, Reply('Done', task_outcome='completed')), 'finished')
        completion = json.loads((store.directory(1) / 'completion.json').read_text(encoding='utf-8'))
        self.assertEqual(completion['verdict'], 'recorded')
        self.assertFalse(completion['passed'])
        graded = regression.grade(None, case, 'Done', store.directory(1) / 'trial_output')
        self.assertFalse(graded['passed'])
        self.assertTrue(all(item['status'] == 'unverified' for item in graded['items']))


if __name__ == '__main__':
    unittest.main()
