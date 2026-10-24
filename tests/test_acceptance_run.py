"""Real acceptance flow: only a declared check on current artifacts establishes a pass."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from agent_harness import make_agent
from agent_core import verification


class RunBeforeReady(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'cato'
        self.root.mkdir()
        self.agent = make_agent('cato', self.root)
        self.store = self.agent._background
        self.task = self.store.start(1, 'Check delivered document')['id']
        self.artifact = self.root / 'delivered.txt'
        self.artifact.write_text('launch works\ndrag works\n', encoding='utf-8')
        self.items = [dict(item='launch result', check='Required launch result text', critical=True, early=True,
                           verification=dict(kind='contains', paths=[str(self.artifact)], text='launch works')),
                      dict(item='drag result', check='Required drag result text', critical=True,
                           verification=dict(kind='contains', paths=[str(self.artifact)], text='drag works'))]
        self.store.add_delegation(self.task, 'vit', 'req-test', 'document', 'owner_decision')
        self.store.record_submission('req-test', self.task, 'vit', 'engineering', 0, self.items)
        self.store.mark_delegation(self.task, 'req-test', 'returned')
        self.returned = self.store.delegation_returned_at('req-test', self.task)
        import delegation_contract
        patcher = patch.object(delegation_contract, 'BRIEFS', self.root / 'briefs')
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_check(self, number):
        return json.loads(self.agent._execute_tool_unguarded('verify_requirement',
            dict(task_id=self.task, request_id='req-test', item=number), 1))

    def accept(self, how='ran'):
        values = [dict(item=n, verdict='met', how=how, evidence='Agent description alone is not evidence.') for n in (1, 2)]
        return self.agent.tool_delegate(action='accept', chat_id=1, agent='vit', request_id='req-test',
                                       task_id=self.task, verdicts=values,
                                       brief_lesson='The actual check failed; correct the artifact before calling it ready.')

    def receipt(self, tool, failed=False):
        folder = self.agent.RUNTIME_DIR / 'task_state' / 'actions'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f'{int((self.returned + 1) * 1e9)}.json').write_text(
            json.dumps(dict(job=self.task, tool=tool, status='error' if failed else 'finished')), encoding='utf-8')

    def test_unrelated_success_is_not_proof(self):
        self.receipt('read_file')
        self.assertNotIn('"accepted": true', self.accept())
        self.assertTrue(self.store.unread_delegations(self.task))

    def test_failed_action_is_not_proof(self):
        self.receipt('execute_command', failed=True)
        self.assertNotIn('"accepted": true', self.accept())
        self.assertTrue(self.store.unread_delegations(self.task))

    def test_each_requirement_needs_its_own_check(self):
        self.assertEqual(self.run_check(1)['status'], 'passed')
        self.assertNotIn('"accepted": true', self.accept())
        self.assertTrue(self.store.unread_delegations(self.task))

    def test_complete_flow_survives_reloading_and_records_passed(self):
        self.assertEqual(self.run_check(1)['status'], 'passed')
        self.assertEqual(self.run_check(2)['status'], 'passed')
        from background_runtime import TaskStore
        self.agent._background = TaskStore(self.root)
        result = json.loads(self.accept())
        self.assertEqual(result['critical_unmet'], [])
        self.assertEqual(result['scorecard']['items_met'], 2)
        self.assertFalse(self.store.unread_delegations(self.task))
        self.assertIn('passed: 1 launch result, 2 drag result', self.store.unrun_line(self.task))

    def test_artifact_change_invalidates_proof_and_delivery_summary(self):
        self.run_check(1)
        self.run_check(2)
        json.loads(self.accept())
        self.artifact.write_text('changed build', encoding='utf-8')
        self.assertNotIn('"accepted": true', self.accept())
        self.assertIn('unverified: 1 launch result, 2 drag result', self.store.unrun_line(self.task))

    def test_failed_requirement_cannot_be_overridden_by_met(self):
        self.artifact.write_text('launch works only', encoding='utf-8')
        self.run_check(1)
        self.assertEqual(self.run_check(2)['status'], 'failed')
        result = json.loads(self.accept())
        self.assertEqual(result['critical_unmet'], [2])
        self.assertEqual(result['scorecard']['items_met'], 1)
        self.assertIn('failed: 2 drag result', self.store.unrun_line(self.task))

    def test_report_is_reviewed_but_never_counts_as_pass(self):
        result = json.loads(self.accept('report'))
        self.assertTrue(result['accepted'])
        self.assertEqual(result['critical_unmet'], [1, 2])
        self.assertEqual(result['scorecard']['items_met'], 0)
        self.assertIn('unverified:', self.store.unrun_line(self.task))

    def test_direct_store_call_cannot_bypass_the_check(self):
        self.assertRaises(ValueError, self.store.accept_delegation, self.task, 'req-test',
                          [dict(item=1, verdict='met', how='ran'), dict(item=2, verdict='met', how='ran')])
        self.assertTrue(self.store.unread_delegations(self.task))

    def test_rework_preserves_proof_for_unchanged_requirements_and_files(self):
        self.run_check(1)
        self.run_check(2)
        self.store.bump_rework('req-test')
        self.assertIn('"accepted": true', self.accept())

    def test_another_requirement_receipt_cannot_be_reused(self):
        self.run_check(1)
        source = verification.receipt_path(self.store, self.task, 'req-test', 1)
        dest = verification.receipt_path(self.store, self.task, 'req-test', 2)
        dest.write_bytes(source.read_bytes())
        self.assertNotIn('"accepted": true', self.accept())

    def test_missing_and_malformed_receipts_are_unverified(self):
        path = verification.receipt_path(self.store, self.task, 'req-test', 1)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('not JSON', encoding='utf-8')
        self.assertNotIn('"accepted": true', self.accept())

    def test_stored_evidence_and_provenance_come_from_the_check(self):
        self.run_check(1)
        self.run_check(2)
        result = json.loads(self.accept('ran'))
        saved = json.loads(result['scorecard']['verdicts'])
        self.assertEqual(saved[0]['how'], 'read')
        self.assertTrue(Path(saved[0]['evidence']).is_file())
        self.assertEqual(saved[0]['assessment'], 'Agent description alone is not evidence.')


if __name__ == '__main__':
    unittest.main()
