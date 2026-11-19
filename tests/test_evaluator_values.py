"""Value semantics and the release that actually generated benchmark artifacts."""
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from tabularium.core.agent_runtime import DurableInbox
from tabularium.learning import regression
from tabularium.ops import release


class EvaluatorValues(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def check_value(self, actual, expected, **fields):
        (self.root / 'result.json').write_text(json.dumps(actual), encoding='utf-8')
        return regression.artifact_check(dict(kind='json', path='result.json', expected=expected, **fields), self.root)['status']

    def test_equivalent_numbers_pass_at_every_depth(self):
        for actual, expected in [(20.0, 20), (20, 20.0), (-0.0, 0),
                                 ({'growth': [20.0, {'n': 2}]}, {'growth': [20, {'n': 2.0}]})]:
            self.assertEqual(self.check_value(actual, expected), 'passed', (actual, expected))

    def test_boolean_string_null_and_container_types_stay_distinct(self):
        for actual, expected in [(False, 0), (0, False), (True, 1.0), (1, True),
                                 ({'x': [False]}, {'x': [0]}), ('20', 20), (None, 0),
                                 ([20], 20), ([20, 30], [20]), ({'a': 20}, {'b': 20}), (20.01, 20)]:
            self.assertEqual(self.check_value(actual, expected), 'failed', (actual, expected))

    def test_integer_requirements_enforce_integer_representation(self):
        for actual, status in [(20, 'passed'), (20.0, 'failed'), (20.5, 'failed'), (True, 'failed'), ('20', 'failed')]:
            self.assertEqual(self.check_value(actual, 20, type='integer'), status, actual)

    def test_nonfinite_numbers_cannot_pass(self):
        for value in [float('nan'), float('inf'), float('-inf')]:
            self.assertEqual(self.check_value(value, value), 'failed')

    def test_numeric_contract_is_validated_before_running(self):
        base = regression.load_cases(self.root)[0]
        for fields in [dict(type='integre'), dict(type='integer', expected=20.5),
                       dict(type='integer', expected=True), dict(type='number', expected='20')]:
            case = copy.deepcopy(base)
            case['requirements'][0]['check'].update(fields)
            self.assertRaises(ValueError, regression.validate_case, case)

    def test_invoice_contract_declares_and_enforces_integer_pence(self):
        case = regression.load_cases(self.root)[0]
        for value, expected_pass in [(4550, True), (4550.0, False), (True, False)]:
            data = dict(gross_pence=value, refund_pence=750, net_pence=3800, currency='GBP')
            (self.root / 'result.json').write_text(json.dumps(data), encoding='utf-8')
            self.assertEqual(regression.grade(None, case, '', self.root)['passed'], expected_pass)

    def test_workbook_numbers_use_the_same_value_and_integer_rules(self):
        from openpyxl import Workbook
        book = Workbook()
        self.addCleanup(book.close)
        book.active['A1'] = 20
        book.save(self.root / 'result.xlsx')
        check = dict(kind='xlsx', path='result.xlsx', sheet='Sheet', cell='A1', expected=20.0)
        self.assertEqual(regression.artifact_check(check, self.root)['status'], 'passed')
        book.active['A1'] = True
        book.save(self.root / 'result.xlsx')
        self.assertEqual(regression.artifact_check(check, self.root)['status'], 'failed')
        book.active['A1'] = 20.5
        book.save(self.root / 'result.xlsx')
        check.update(expected=20.5, type='integer')
        self.assertEqual(regression.artifact_check(check, self.root)['status'], 'failed')


class EvaluatorRelease(unittest.TestCase):
    CONTROLLER = dict(commit='controller', digest='controller-digest')
    WORKER = dict(commit='tested-worker', digest='tested-digest')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.case = regression.load_cases(self.root)[0]
        p = patch.object(release, 'LOADED', self.CONTROLLER)
        p.start()
        self.addCleanup(p.stop)

    def attempt(self, number, identities):
        folder = self.root / str(number)
        folder.mkdir()
        output = folder / 'trial_output'
        output.mkdir()
        (output / 'result.json').write_text(json.dumps(dict(gross_pence=4550, refund_pence=750,
                                                         net_pence=3800, currency='GBP')), encoding='utf-8')
        inbox = DurableInbox(folder, lambda *args: None)
        for identity in identities:
            inbox.health['release'] = identity
            event = inbox.record_event('response', 'model', 'test', 'anthropic',
                                       dict(input_tokens=10, cache_read_input_tokens=0,
                                            cache_creation_input_tokens=0, output_tokens=5))
            self.assertIsNotNone(event)
        return dict(task=number, status='finished', folder=folder)

    def evaluate(self, identities):
        record = regression.run(self.root, None, 1, [self.case], None,
                                executor=lambda case, n: self.attempt(n, identities), log=lambda s: None)
        self.assertTrue(record['cases'][0]['attempts'][0]['passed'])
        return json.loads(Path(record['path']).read_text(encoding='utf-8'))

    def test_real_snapshot_without_git_records_its_loaded_identity(self):
        package = self.root / 'snapshot/src/tabularium'
        shutil.copytree(Path(regression.__file__).resolve().parents[1], package,
                        ignore=shutil.ignore_patterns('__pycache__'))
        (package / 'RELEASE.json').write_text(json.dumps(self.WORKER), encoding='utf-8')
        script = """import json,sqlite3,sys
from pathlib import Path
from tabularium.core.agent_runtime import DurableInbox
from tabularium.ops import release
root=Path(sys.argv[1]);root.mkdir()
inbox=DurableInbox(root,lambda *args:None)
inbox.record_event('response','model','test','anthropic',dict(input_tokens=1,output_tokens=1))
db=sqlite3.connect(inbox.database)
try:
 metrics=json.loads(db.execute('SELECT metrics FROM usage_events').fetchone()[0])
finally:
 db.close()
print(json.dumps(dict(checkout_commit=release.commit(),receipt=metrics.get('release'))))
"""
        env = dict(os.environ, PYTHONPATH=str(package.parent), TABULARIUM_HOME=str(self.root))
        result = subprocess.run([sys.executable, '-B', '-c', script, str(self.root / 'attempt')],
                                cwd=self.root, env=env, capture_output=True, text=True, timeout=30,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), dict(checkout_commit='', receipt=self.WORKER))

    def test_run_cannot_hide_different_versions_across_attempts(self):
        other = dict(commit='other-worker', digest='other-digest')
        def execute(case, n):
            return self.attempt(n, [self.WORKER if n == 1 else other])
        record = regression.run(self.root, None, 1, [self.case], None, k=2,
                                executor=execute, log=lambda s: None)
        self.assertIsNone(record['release'])
        self.assertEqual([a['releases'] for a in record['cases'][0]['attempts']], [[self.WORKER], [other]])

    def test_usage_receipt_records_loaded_snapshot_without_a_git_checkout(self):
        outcome = self.attempt(1, [self.WORKER])
        import sqlite3
        db = sqlite3.connect(outcome['folder'] / 'task_state/inbox.sqlite3')
        try:
            metrics = json.loads(db.execute("SELECT metrics FROM usage_events WHERE kind='response'").fetchone()[0])
        finally:
            db.close()
        self.assertEqual(metrics.get('release'), self.WORKER)

    def test_persisted_run_identifies_worker_separately_from_evaluator(self):
        record = self.evaluate([self.WORKER])
        self.assertEqual(record['release'], self.WORKER)
        self.assertEqual(record['evaluator_release'], self.CONTROLLER)
        self.assertEqual(record['cases'][0]['attempts'][0]['releases'], [self.WORKER])

    def test_mixed_worker_versions_are_preserved_without_claiming_one_tested_version(self):
        other = dict(commit='other-worker', digest='other-digest')
        record = self.evaluate([self.WORKER, other])
        self.assertIsNone(record['release'])
        self.assertEqual(record['cases'][0]['attempts'][0]['releases'], [self.WORKER, other])

    def test_unknown_worker_version_is_not_replaced_by_controller_version(self):
        record = self.evaluate([None])
        self.assertIsNone(record['release'])
        self.assertEqual(record['evaluator_release'], self.CONTROLLER)
        self.assertEqual(record['cases'][0]['attempts'][0]['releases'], [None])

    def test_missing_usage_cannot_claim_a_tested_worker_version(self):
        record = self.evaluate([])
        self.assertIsNone(record['release'])
        self.assertEqual(record['cases'][0]['attempts'][0]['releases'], [])
