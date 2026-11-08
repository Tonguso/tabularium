"""Ordinary task proof and context have one owner, independent of unrelated obligations."""
import json
import tempfile
import unittest
from pathlib import Path
from tests.agent_harness import make_agent
from tabularium.records import verification
from tabularium.learning import regression
from tabularium.core.roster import AGENTS
from tabularium.core.reply_envelope import Reply

class EfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def agent(self, name):
        root = self.root / name
        root.mkdir()
        return make_agent(name, root, background=True)

    def item(self, agent, text, source=0):
        path = agent._background.directory(1) / (text + '.txt')
        path.write_text(text, encoding='utf-8')
        return dict(item=text, source=source, check='Read exact required text', critical=True,
                    verification=dict(kind='contains', paths=[str(path)], text=text))

    def declare(self, agent, items):
        return agent._background.checkpoint(1, 1, 'Saved progress', 'Continue', [], guesses=['none'], completion_items=items)

    def test_later_work_keeps_unrelated_proof_but_blocks_completion_for_every_agent(self):
        for name in AGENTS:
            agent = self.agent(name); store = agent._background
            first = self.item(agent, 'towers')
            self.declare(agent, [first])
            verification.execute(agent, 1, 1, 1)
            store.control(1, 1, 'steer', 'Also deliver carriers')
            source = store.pending_controls(1)[0]['id']
            store.acknowledge([source])
            self.assertEqual(verification.status(store, 1, '', 1), 'passed', name)
            second = self.item(agent, 'carriers', source)
            self.declare(agent, [second])
            self.assertEqual(verification.status(store, 1, '', 1), 'passed', name)
            self.assertEqual(store.finish(1, Reply('Done', task_outcome='completed')), 'needs_attention')
            store.control(1, 1, 'resume')
            verification.execute(agent, 1, 1, 2)
            self.assertEqual(store.finish(1, Reply('Done', task_outcome='completed')), 'finished')
            Path(first['verification']['paths'][0]).write_text('changed', encoding='utf-8')
            self.assertEqual(verification.status(store, 1, '', 1), 'unverified')
            self.assertEqual(verification.status(store, 1, '', 2), 'passed')

    def test_test_edit_invalidates_only_its_own_proof(self):
        agent = self.agent('cato'); store = agent._background
        first, second = self.item(agent, 'towers'), self.item(agent, 'carriers')
        self.declare(agent, [first, second])
        for n in (1, 2): verification.execute(agent, 1, 1, n)
        first['verification']['text'] = 'stricter assertion'
        self.declare(agent, [dict(first, id=1)])
        self.assertEqual(verification.status(store, 1, '', 1), 'unverified')
        self.assertEqual(verification.status(store, 1, '', 2), 'passed')

    def test_real_resumed_worker_does_not_copy_checkpoint_into_saved_history(self):
        from tests.test_worker import STUB
        from tabularium.records.background_runtime import TaskStore
        root = self.root / 'worker'; root.mkdir()
        (root / 'agent.py').write_text(STUB, encoding='utf-8')
        store = TaskStore(root)
        ident = store.start(1, 'Synthetic resume')['id']
        folder = store.directory(ident)
        (folder / 'conversation_1.json').write_text('[]')
        store.checkpoint(1, ident, 'UNIQUE SAVED PROGRESS', 'Continue', [], facts=['UNIQUE FACT'])
        store.launch_next()
        process = store.processes[ident]
        try:
            process.wait(timeout=10)
            saved = (folder / 'conversation_1.json').read_text()
            self.assertNotIn('UNIQUE SAVED PROGRESS', saved)
            self.assertNotIn('Saved task checkpoint', saved)
            self.assertIn('UNIQUE FACT', store.contract(ident))
        finally:
            if process.poll() is None:
                process.terminate(); process.wait(timeout=5)

    def test_unchanged_proof_is_reused_without_running_the_tool_again(self):
        for name in AGENTS:
            agent = self.agent(name); store = agent._background
            self.declare(agent, [self.item(agent, 'towers')])
            verification.execute(agent, 1, 1, 1)
            path = verification.receipt_path(store, 1, '', 1)
            before = path.read_bytes()
            def unexpected(*args):
                self.fail('An unchanged passing requirement executed its tool again')
            agent._execute_tool_unguarded = unexpected
            result = json.loads(verification.execute(agent, 1, 1, 1))
            self.assertEqual(result['status'], 'passed')
            self.assertTrue(result['reused'])
            self.assertEqual(path.read_bytes(), before)

    def test_checkpoint_facts_and_decisions_are_available_in_the_one_current_context(self):
        agent = self.agent('cato'); store = agent._background
        store.checkpoint(1, 1, 'FILE SAVED', 'CONTINUE', ['saved.txt'], facts=['KNOWN FACT'],
                         guesses=['OPEN ASSUMPTION'], decisions=['Alex: "USE BLUE"'])
        text = store.contract(1)
        for value in ['FILE SAVED', 'KNOWN FACT', 'OPEN ASSUMPTION', 'USE BLUE']:
            self.assertEqual(text.count(value), 1, value)

    def test_requirement_changed_during_check_cannot_report_passed(self):
        agent = self.agent('cato')
        item = self.item(agent, 'towers')
        self.declare(agent, [item])
        original = agent._execute_tool_unguarded
        def changed(name, inputs, chat):
            result = original(name, inputs, chat)
            item['verification']['text'] = 'new assertion'
            self.declare(agent, [dict(item, id=1)])
            return result
        agent._execute_tool_unguarded = changed
        result = json.loads(verification.execute(agent, 1, 1, 1))
        self.assertEqual(result['status'], 'unverified')

    def test_each_real_turn_keeps_old_proof_when_later_work_arrives(self):
        from types import SimpleNamespace as NS
        for name in AGENTS:
            agent = self.agent(name); store = agent._background
            first = self.item(agent, 'towers')
            second = self.item(agent, 'carriers')
            self.declare(agent, [first])
            calls = []
            def exchange(client, *, model, instructions, input_items, tools, plan_mode):
                calls.append(input_items)
                step = len(calls)
                if step == 1:
                    tool, args = 'verify_requirement', dict(task_id=1, item=1)
                elif step == 2:
                    self.assertEqual(verification.status(store, 1, '', 1), 'passed')
                    store.control(1, 1, 'steer', 'Also deliver carriers')
                    source = store.pending_controls(1)[0]['id']
                    store.acknowledge([source])
                    second['source'] = source
                    tool, args = 'task_checkpoint', dict(completed='Declared added file', next_action='Verify it', evidence=[], completion_items=[second])
                elif step == 3:
                    self.assertEqual(verification.status(store, 1, '', 1), 'passed')
                    self.assertIn('carriers', json.dumps(input_items))
                    tool, args = 'verify_requirement', dict(task_id=1, item=2)
                else:
                    output = NS(type='message', content=[NS(type='output_text', text='{"reply":"Both files checked","task_outcome":"completed"}')])
                    return NS(output=[output], status='completed', incomplete_details=None, usage=None), '', [], []
                output = NS(type='function_call', name=tool, call_id='call-'+str(step), arguments=json.dumps(args))
                return NS(output=[output], status='completed', incomplete_details=None, usage=None), '', [], []
            agent._responses_call = exchange
            token = agent._routing.ACTIVE.set(dict(kind='openai_api', model='test-model'))
            try:
                result = agent.run_turn_openai([dict(role='user', content='Complete the declared files.')], 1)
            finally:
                agent._routing.ACTIVE.reset(token)
            self.assertEqual(len(calls), 4)
            self.assertEqual(store.finish(1, result), 'finished')

    def test_current_legacy_proof_migrates_before_an_unrelated_addition(self):
        import hashlib
        agent = self.agent('cato'); store = agent._background
        self.declare(agent, [self.item(agent, 'towers')])
        verification.execute(agent, 1, 1, 1)
        path = verification.receipt_path(store, 1, '', 1)
        record = json.loads(path.read_text())
        record.pop('requirement')
        items, generation = verification.contract(store, 1)
        record['contract'] = hashlib.sha256(json.dumps([items, generation], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        path.write_text(json.dumps(record))
        self.assertEqual(verification.status(store, 1, '', 1), 'passed')
        self.declare(agent, [self.item(agent, 'carriers')])
        self.assertEqual(verification.status(store, 1, '', 1), 'passed')

    def test_progress_acknowledgement_does_not_repeat_the_contract(self):
        agent = self.agent('cato')
        item = self.item(agent, 'unique-requirement-text')
        result = self.declare(agent, [item])
        self.assertNotIn('completion_items', result)
        self.assertNotIn(item['item'], json.dumps(result))
        self.assertIn(item['item'], agent._background.contract(1))

    def test_progress_checkpoint_has_no_role_based_reporting_gate(self):
        for name in AGENTS:
            agent = self.agent(name)
            result = agent._execute_tool_unguarded('task_checkpoint', dict(completed='Saved file', next_action='Continue', evidence=[]), 1)
            self.assertNotIn('not saved', str(result), name)
            self.assertTrue((agent._background.directory(1) / 'checkpoint.json').exists())

    def test_preset_verification_is_not_a_second_worker_workflow(self):
        for name in AGENTS:
            agent = self.agent(name); store = agent._background
            case = regression.load_cases(store.agent_dir)[0]
            (store.directory(1) / 'context.json').write_text(json.dumps({'eval': {'case': case['id'], 'benchmark': regression.fingerprint([case])}}))
            for provider in ('anthropic', 'openai'):
                tools = {(t.get('function') or t)['name']: t.get('function') or t for t in agent.get_tools(provider)}
                self.assertNotIn('verify_requirement', tools)
                schema = tools['task_checkpoint'].get('input_schema') or tools['task_checkpoint']['parameters']
                self.assertNotIn('completion_items', schema['properties'])
            result = agent._execute_tool_unguarded('verify_requirement', dict(task_id=1, item=1), 1)
            self.assertIn('not available', str(result))

if __name__ == '__main__':
    unittest.main()
