"""Durable obligations survive progress edits, restarts and later instructions."""
import json
from pathlib import Path
import tempfile
import unittest
from tests.agent_harness import make_agent
from tabularium.records import verification
from tabularium.core.roster import AGENTS
from tabularium.core.reply_envelope import Reply
from tabularium.records.background_runtime import TaskStore

class RequirementsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def setup_agent(self, name):
        root = self.root / name
        root.mkdir()
        agent = make_agent(name, root, background=True)
        path = root / 'delivered.txt'
        path.write_text('towers', encoding='utf-8')
        self.path = path
        return agent

    def item(self, text, source=0):
        return {'item': text, 'source': source, 'check': 'Inspect delivered text', 'critical': True,
                'verification': {'kind': 'contains', 'paths': [str(self.path)], 'text': text}}

    def checkpoint(self, agent, items):
        return agent._background.checkpoint(1, 1, '', '', [], guesses=['none'], completion_items=items)

    def test_omission_cannot_erase_an_unfinished_requirement_in_any_role(self):
        for name in AGENTS:
            agent = self.setup_agent(name)
            self.checkpoint(agent, [self.item('towers'), self.item('carriers')])
            self.checkpoint(agent, [self.item('towers')])
            agent._execute_tool_unguarded('verify_requirement', {'task_id': 1, 'item': 1}, 1)
            self.assertEqual(agent._background.finish(1, Reply('Done', task_outcome='completed')), 'needs_attention', name)
            restored = TaskStore(agent._background.agent_dir, agent._background.role)
            self.assertEqual([x['item'] for x in verification.contract(restored, 1)[0]], ['towers', 'carriers'])

    def test_later_instruction_stays_listed_as_uncovered_guidance_but_does_not_block_finishing(self):
        # 2026-10-24: coverage is guidance in the worker's context; completion rests on declared checks only.
        agent = self.setup_agent('cato')
        store = agent._background
        self.checkpoint(agent, [self.item('towers')])
        store.control(1, 1, 'steer', 'Also deliver carriers')
        added = store.pending_controls(1)[0]['id']
        store.acknowledge([added])
        self.checkpoint(agent, [self.item('towers')])
        self.assertIn('Instructions without declared requirements: [%d]' % added, store.contract(1))
        agent._execute_tool_unguarded('verify_requirement', {'task_id': 1, 'item': 1}, 1)
        self.assertEqual(store.finish(1, Reply('Done', task_outcome='completed')), 'finished')

    def test_delegated_rescope_cannot_shrink_the_contract(self):
        agent = self.setup_agent('cato')
        store = agent._background
        items = [self.item('towers'), self.item('carriers')]
        store.record_submission('r1', 1, 'vit', 'engineering', 1000, items)
        store.bump_rework('r1', items=items[:1], count=False)
        self.assertEqual(len(store.contract_items('r1')), 2)

    def test_later_instruction_is_retained_and_verified_before_all_roles_finish(self):
        for name in AGENTS:
            agent = self.setup_agent(name)
            store = agent._background
            self.checkpoint(agent, [self.item('towers')])
            store.control(1, 1, 'steer', 'Also deliver carriers')
            added = store.pending_controls(1)[0]['id']
            store.acknowledge([added])
            self.checkpoint(agent, [self.item('carriers', added)])
            # A later partial checkpoint cannot change the ordering or remove towers.
            self.checkpoint(agent, [self.item('carriers', added)])
            self.assertEqual([x['item'] for x in verification.contract(store, 1)[0]], ['towers', 'carriers'])
            self.path.write_text('towers carriers', encoding='utf-8')
            for n in (1, 2):
                agent._execute_tool_unguarded('verify_requirement', {'task_id': 1, 'item': n}, 1)
            self.assertEqual(store.finish(1, Reply('Done', task_outcome='completed')), 'finished', name)

    def test_renaming_rebinding_and_unknown_sources_are_refused(self):
        agent = self.setup_agent('cato')
        self.checkpoint(agent, [self.item('towers')])
        self.assertRaises(ValueError, self.checkpoint, agent, [dict(self.item('carriers'), id=1)])
        self.assertRaises(ValueError, self.checkpoint, agent, [self.item('carriers', 999)])
        store = agent._background
        store.control(1, 1, 'steer', 'Also deliver carriers')
        added = store.pending_controls(1)[0]['id']
        store.acknowledge([added])
        self.assertRaises(ValueError, self.checkpoint, agent, [dict(self.item('towers', added), id=1)])
        self.assertEqual(verification.uncovered_sources(store, 1, verification.contract(store, 1)[0]), [added])

    def test_legacy_checkpoint_migrates_once_and_cannot_overwrite_the_contract(self):
        agent = self.setup_agent('cato')
        store = agent._background
        cp = store.directory(1) / 'checkpoint.json'
        cp.write_text(json.dumps({'completion_items': [self.item('towers'), self.item('carriers')]}))
        self.assertEqual(len(verification.contract(store, 1)[0]), 2)
        cp.write_text(json.dumps({'completion_items': [self.item('towers')]}))
        self.assertEqual(len(verification.contract(TaskStore(store.agent_dir), 1)[0]), 2)
        self.checkpoint(agent, [self.item('towers')])
        self.assertNotIn('completion_items', json.loads(cp.read_text()))

    def test_fixed_benchmark_uses_its_existing_contract_without_a_second_checklist(self):
        agent = self.setup_agent('cato')
        from tabularium.learning import regression
        store = agent._background
        case = regression.load_cases(store.agent_dir)[0]
        (store.directory(1) / 'context.json').write_text(json.dumps({'eval': {
            'case': case['id'], 'benchmark': regression.fingerprint([case])}}))
        text = store.contract(1)
        self.assertIn('Preset acceptance criteria', text)
        self.assertNotIn('Instructions without declared requirements', text)
        self.assertNotIn('4550', text)  # do not reveal oracle answers to the worker

    def test_explicit_next_id_can_add_an_obligation_on_both_provider_schemas(self):
        for name in AGENTS:
            agent = self.setup_agent(name)
            self.checkpoint(agent, [self.item('towers')])
            for provider in ('anthropic', 'openai'):
                tools = agent.get_tools(provider)
                self.assertTrue(any((t.get('function') or t).get('name') == 'task_checkpoint' for t in tools))
            self.checkpoint(agent, [dict(self.item('carriers'), id=2)])
            self.assertEqual(len(verification.contract(agent._background, 1)[0]), 2)
            self.checkpoint(agent, [dict(self.item('carriers'), id=2)])
            self.assertEqual(len(verification.contract(agent._background, 1)[0]), 2)

    def test_status_and_budget_views_use_the_durable_contract(self):
        agent = self.setup_agent('cato')
        self.checkpoint(agent, [self.item('towers'), self.item('carriers')])
        from tabularium.records import task_cost
        self.assertEqual(len(task_cost.contract(agent._background.directory(1))['completion_items']), 2)

    def test_initial_contract_is_saved_before_worker_launch(self):
        agent = self.setup_agent('cato')
        store = agent._background
        seen = []
        def launch():
            seen.extend(verification.contract(store, 2)[0])
            return None
        store.monitoring = True
        store.launch_next = launch
        task = store.start(1, 'Deliver both baskets', completion_items=[self.item('towers'), self.item('carriers')])
        self.assertEqual(task['id'], 2)
        self.assertEqual([x['item'] for x in seen], ['towers', 'carriers'])
        store.checkpoint(1, 2, '', '', [], completion_items=[self.item('towers')])
        self.assertEqual(len(verification.contract(store, 2)[0]), 2)

    def test_delegated_brief_seeds_requirements_and_preserves_parent_references(self):
        agent = self.setup_agent('vitruvius')
        store = agent._background
        items = verification.delegated_items([dict(self.item('towers', 99), id=7), self.item('carriers')])
        (store.directory(1) / 'context.json').write_text(json.dumps({'contract': {'completion_items': items}}))
        self.checkpoint(agent, [self.item('towers')])
        saved = verification.contract(store, 1)[0]
        self.assertEqual(len(saved), 2)
        self.assertEqual(saved[0]['parent_item'], 7)
        self.assertEqual(saved[0]['parent_source'], 99)

if __name__ == '__main__':
    unittest.main()
