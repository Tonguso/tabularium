"""Context ownership: task requirements survive; unrelated task state does not travel."""
import json
from pathlib import Path
import tempfile
import unittest
from tests.agent_harness import make_agent, put
from background_runtime import TaskStore
from agent_core.task_decisions import answer_record
from agent_core.roster import AGENTS


class ContextLoadingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_worker_scope_and_full_contract_for_every_role(self):
        for name in AGENTS:
            root = self.root / name
            root.mkdir()
            agent = make_agent(name, root, background=True)
            store = agent._background
            other = store.start(1, 'UNRELATED PROJECT', '')['id']
            store.ask(1, other, 'UNRELATED DECISION')
            brief = 'Required detail. ' * 700 + 'FINAL REQUIREMENT'
            store.control(1, 1, 'steer', brief)
            store.acknowledge([x['id'] for x in store.pending_controls(1)])
            decision = store.ask(1, 1, 'Choose a colour')
            answer_record(store, 1, decision['id'], 'USE BLUE', 'fixture verified user quote')
            agent._background = TaskStore(root, store.role)
            store = agent._background
            store.acknowledge([x['id'] for x in store.pending_controls(1)])
            store.checkpoint(1, 1, 'Draft saved', 'CHECK THE SAVED FILE', 'fixture', guesses=[],
                             completion_items=[{'item': 'SAVE THE WORKBOOK', 'check': 'Inspect cells', 'critical': True}])
            messages = agent.load_memory_context() + [{'role': 'user', 'content': 'Continue'}]
            short = json.dumps(agent.trim_history(messages, 1))
            self.assertNotIn('UNRELATED DECISION', short)
            self.assertNotIn('UNRELATED PROJECT', short)
            self.assertIn('FINAL REQUIREMENT', short)
            self.assertIn('USE BLUE', short)
            self.assertIn('SAVE THE WORKBOOK', short)
            long = messages + [{'role': 'assistant' if i % 2 else 'user', 'content': str(i)} for i in range(80)]
            trimmed = agent._prepare_request_context(agent.trim_history(long, 1), 1)
            wire = json.dumps(trimmed)
            self.assertIn('FINAL REQUIREMENT', wire)
            self.assertIn('USE BLUE', wire)
            self.assertIn('SAVE THE WORKBOOK', wire)
            self.assertIn('CHECK THE SAVED FILE', wire)
            self.assertNotIn('FINAL REQUIREMENT', json.dumps(messages))
            agent.BACKGROUND_TASK_ID = ''
            self.assertIn('UNRELATED DECISION', json.dumps(agent.trim_history(messages, 1)))

    def test_worker_memory_keeps_preferences_but_not_other_projects(self):
        agent = make_agent('cato', self.root, background=True)
        (self.root / 'memory.md').write_text('# Memory\n## Core\nUse integer pence.\n', encoding='utf-8')
        from agent_core.memory_context import WorkingMemory
        WorkingMemory(self.root).thread('OTHER PROJECT', 'UNRELATED NEXT STEP')
        self.assertIn('Use integer pence', agent._load_core_memory())
        self.assertNotIn('UNRELATED NEXT STEP', agent._load_core_memory())
        agent.BACKGROUND_TASK_ID = ''
        self.assertIn('UNRELATED NEXT STEP', agent._load_core_memory())

    def test_request_measurement_contains_sizes_not_private_text(self):
        agent = make_agent('tacitus', self.root, background=True)
        messages = agent.load_memory_context() + [{'role': 'user', 'content': 'PRIVATE SENTINEL'}]
        agent._prepare_request_context(agent.trim_history(messages, 1), 1)
        path = Path(agent.RUNTIME_DIR) / 'task_state' / 'context_sizes.json'
        self.assertTrue(path.exists())
        record = json.loads(path.read_text(encoding='utf-8'))
        self.assertGreater(record['system_chars'], 0)
        self.assertGreater(record['messages_chars'], len('PRIVATE SENTINEL'))
        self.assertNotIn('PRIVATE SENTINEL', json.dumps(record))
        self.assertTrue(all(type(v) is int and v >= 0 for v in record.values()))

    def test_skill_catalogue_is_retrievable_without_loading_all_descriptions(self):
        agent = make_agent('cato', self.root)
        folder = self.root / 'skills' / 'opaque-method'
        folder.mkdir(parents=True)
        (folder / 'SKILL.md').write_text('---\nname: opaque-method\ndescription: EXACT DISCOVERY DESCRIPTION\n---\nEXACT METHOD BODY', encoding='utf-8')
        index = agent.skills_prompt_block()
        self.assertIn('opaque-method', index)
        self.assertNotIn('EXACT DISCOVERY DESCRIPTION', index)
        self.assertIn('EXACT DISCOVERY DESCRIPTION', agent.tool_skill(''))
        self.assertIn('EXACT METHOD BODY', agent.tool_skill('opaque-method'))
        agent.ALWAYS_SKILLS = ('opaque-method',)
        self.assertIn('EXACT METHOD BODY', agent.skills_prompt_block())


if __name__ == '__main__':
    unittest.main()
