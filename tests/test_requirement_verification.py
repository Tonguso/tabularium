"""Checks run through the real shared agent dispatcher and respect each role."""
import json
import tempfile
import sys
import unittest
from pathlib import Path
from agent_harness import make_agent, AGENTS
from tabularium.records import verification
class RequirementChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def setup_agent(self, name, spec=None):
        root = self.root / name
        root.mkdir(exist_ok=True)
        agent = make_agent(name, root, background=True)
        artifact = root / 'delivered.txt'
        artifact.write_text('expected value', encoding='utf-8')
        requirement = dict(item='Expected content', check='Assert the exact expected value', critical=True,
                           verification=spec or dict(kind='contains', paths=[str(artifact)], text='expected value'))
        agent._background.checkpoint(1, 1, 'Declared test', 'Run check', [], guesses=[], completion_items=[requirement])
        return agent, artifact

    def run_check(self, agent):
        return agent._execute_tool_unguarded('verify_requirement', dict(task_id=1, item=1), 1)

    def test_all_four_agents_run_the_same_check_and_report_live_status(self):
        for name in AGENTS:
            agent, artifact = self.setup_agent(name)
            self.assertIn('verify_requirement', [t['name'] for t in agent.get_tools('anthropic')])
            self.assertEqual(json.loads(self.run_check(agent))['status'], 'passed', name)
            self.assertEqual(verification.summary(agent._background, 1), '', name)          # passes are silent (2026-10-24)
            artifact.write_text('different', encoding='utf-8')
            self.assertIn('Not checked on the delivered file:', verification.summary(agent._background, 1))

    def test_command_assertions_pass_and_fail_with_real_process(self):
        agent, artifact = self.setup_agent('cato')
        spec = dict(kind='command', paths=[str(artifact)], argv=[str(Path(sys.executable).with_name('python.exe')) if sys.platform == 'win32' else sys.executable, "-c", "import sys; from pathlib import Path; assert Path(sys.argv[1]).read_text() == 'expected value'", "{artifact}"])
        agent._background.checkpoint(1, 1, '', '', [], completion_items=[dict(id=1, item='Expected content', check='Compare text', critical=True, verification=spec)])
        self.assertEqual(json.loads(self.run_check(agent))['status'], 'passed')
        artifact.write_text('wrong value', encoding='utf-8')
        self.assertEqual(json.loads(self.run_check(agent))['status'], 'failed')
        self.assertEqual(verification.status(agent._background, 1, '', 1), 'failed')

    def test_tacitus_does_not_gain_shell_access(self):
        agent, artifact = self.setup_agent('tacitus')
        spec = dict(kind='command', paths=[str(artifact)], argv=[str(Path(sys.executable).with_name('python.exe')) if sys.platform == 'win32' else sys.executable, '-c', 'pass', '{artifact}'])
        agent._background.checkpoint(1, 1, '', '', [], completion_items=[dict(id=1, item='Expected content', check='Read', critical=True, verification=spec)])
        self.assertIn('cannot use', self.run_check(agent))
        self.assertEqual(verification.status(agent._background, 1, '', 1), 'unverified')

    def test_changed_requirement_invalidates_receipt(self):
        agent, artifact = self.setup_agent('vitruvius')
        self.run_check(agent)
        spec = dict(kind='contains', paths=[str(artifact)], text='something different')
        agent._background.checkpoint(1, 1, '', '', [], guesses=[], completion_items=[dict(id=1, item='Expected content', check='Different text', critical=True, verification=spec)])
        self.assertEqual(verification.status(agent._background, 1, '', 1), 'unverified')

    def test_dependency_change_invalidates_receipt(self):
        agent, artifact = self.setup_agent('cato')
        dependency = self.root / 'test-version.txt'
        dependency.write_text('v1', encoding='utf-8')
        spec = dict(kind='contains', paths=[str(artifact), str(dependency)], text='expected value')
        agent._background.checkpoint(1, 1, '', '', [], completion_items=[dict(id=1, item='Expected content', check='Read text', critical=True, verification=spec)])
        self.run_check(agent)
        dependency.write_text('v2', encoding='utf-8')
        self.assertEqual(verification.status(agent._background, 1, '', 1), 'unverified')

    def test_cross_chat_and_worker_checks_refused(self):
        agent, _ = self.setup_agent('cato')
        self.assertIn('another chat', verification.execute(agent, 99, 1, 1))
        other = agent._background.start(1, 'other task')['id']
        self.assertIn('another chat or worker', verification.execute(agent, 1, other, 1))

    def test_native_command_failure_is_not_hidden_by_powershell(self):
        import sys
        agent, artifact = self.setup_agent('cato')
        spec = dict(kind='command', paths=[str(artifact)], argv=[str(Path(sys.executable).with_name('python.exe')) if sys.platform == 'win32' else sys.executable, "-c", "import sys; print('test completed'); sys.exit(3)", "{artifact}"])
        agent._background.checkpoint(1, 1, '', '', [], completion_items=[dict(id=1, item='Expected content', check='Run assertions', critical=True, verification=spec)])
        self.assertEqual(json.loads(self.run_check(agent))['status'], 'failed')

    def test_refused_and_interrupted_reruns_invalidate_old_pass(self):
        from unittest.mock import patch
        from tabularium.core.outcome import Refused
        agent, _ = self.setup_agent('cato')
        self.assertEqual(json.loads(self.run_check(agent))['status'], 'passed')
        patcher = patch.object(agent, '_execute_tool_unguarded', return_value=Refused('Not permitted'))
        patcher.start()
        try:
            result = verification.execute(agent, 1, 1, 1, rerun=True)
        finally:
            patcher.stop()
        self.assertEqual(json.loads(result)['status'], 'unverified')
        self.assertEqual(verification.status(agent._background, 1, '', 1), 'unverified')
        self.run_check(agent)
        patcher = patch.object(agent, '_execute_tool_unguarded', side_effect=RuntimeError('worker stopped'))
        patcher.start()
        try:
            self.assertRaises(RuntimeError, verification.execute, agent, 1, 1, 1, rerun=True)
        finally:
            patcher.stop()
        self.assertEqual(verification.status(agent._background, 1, '', 1), 'unverified')

    def test_change_during_the_check_is_not_a_pass(self):
        agent, artifact = self.setup_agent('cato')
        spec = dict(kind='command', paths=[str(artifact)], argv=[str(Path(sys.executable).with_name('python.exe')) if sys.platform == 'win32' else sys.executable, "-c", "import sys; from pathlib import Path; Path(sys.argv[1]).write_text('changed')", "{artifact}"])
        agent._background.checkpoint(1, 1, '', '', [], completion_items=[dict(id=1, item='Expected content', check='Test exact bytes', critical=True, verification=spec)])
        self.assertEqual(json.loads(self.run_check(agent))['status'], 'unverified')

    def test_overlapping_checks_cannot_overwrite_each_others_result(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        from tabularium.core.outcome import Refused
        agent, _ = self.setup_agent('cato')
        entered, release = threading.Event(), threading.Event()
        run = agent._execute_tool_unguarded
        calls = []
        def paused(name, inputs, chat):
            calls.append(name)
            if len(calls) == 1:
                entered.set()
                release.wait(5)
            return run(name, inputs, chat)
        pool = ThreadPoolExecutor(max_workers=1)
        patcher = patch.object(agent, '_execute_tool_unguarded', side_effect=paused)
        patcher.start()
        try:
            first = pool.submit(verification.execute, agent, 1, 1, 1)
            self.assertTrue(entered.wait(5))
            second = verification.execute(agent, 1, 1, 1)
            self.assertIsInstance(second, Refused)
            self.assertEqual(verification.status(agent._background, 1, '', 1), 'unverified')
            release.set()
            self.assertEqual(json.loads(first.result(5))['status'], 'passed')
        finally:
            release.set()
            pool.shutdown(wait=True)
            patcher.stop()

    def test_each_agent_model_turn_receives_actual_verification_result(self):
        from types import SimpleNamespace as NS
        for name in AGENTS:
            agent, _ = self.setup_agent(name)
            token = agent._routing.ACTIVE.set(dict(kind='openai_api', model='test-model'))
            calls = []
            def exchange(client, *, model, instructions, input_items, tools, plan_mode):
                calls.append(input_items)
                self.assertIn('verify_requirement', {t.get('name') for t in tools})
                if len(calls) == 1:
                    output = NS(type='function_call', name='verify_requirement', call_id='verify-one',
                                arguments=json.dumps(dict(task_id=1, item=1)))
                else:
                    output = NS(type='message', content=[NS(type='output_text', text='{"reply":"Check recorded"}')])
                return NS(output=[output], status='completed', incomplete_details=None, usage=None), '', [], []
            agent._responses_call = exchange
            messages = [dict(role='user', content='Run the declared requirement check.')]
            try:
                reply = agent.run_turn_openai(messages, 1)
            finally:
                agent._routing.ACTIVE.reset(token)
            self.assertEqual(reply, 'Check recorded')
            results = [b for m in messages if isinstance(m.get('content'), list) for b in m['content']
                       if isinstance(b, dict) and b.get('type') == 'tool_result']
            self.assertEqual(len(results), 1)
            self.assertIn('"status": "passed"', str(results[0]['content']))
            self.assertEqual(verification.status(agent._background, 1, '', 1), 'passed')


if __name__ == '__main__':
    unittest.main()
