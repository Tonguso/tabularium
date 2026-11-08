"""Research outputs are checked against independently worked fixtures, never their own claims."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from tabularium.learning import regression
GOOD = {
    'research-scope-v1': dict(towers=['ALP', 'BRK'], carriers=['CRN', 'DEL'], excluded=['ELM'],
                            source_ids={'ALP':'s1','BRK':'s2','CRN':'s3','DEL':'s4'}),
    'research-sources-v1': dict(revenue_m=120, period='FY2025', source_id='filing-amended',
                              superseded_source_id='filing-original', growth_pct=20,
                              rejected_comparison_source_id='quarter-update'),
    'research-unknowns-v1': dict(measured_input=1000, measured_cached=None, estimated_output=90,
                               measured_output=None, quota_saved_pct=None, cache_hit_rate=None),
    'research-decision-v1': dict(winner_on_price='A', winner_on_tested_reliability='B',
                               deployment_choice=None, missing_evidence=['representative_reliability'])}
CSV = 'basket,ticker,source_id\ntowers,ALP,s1\ntowers,BRK,s2\ncarriers,CRN,s3\ncarriers,DEL,s4\n'

class ResearchBenchmark(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cases = {c['id']: c for c in regression.load_cases(self.root)}

    def case(self, key):
        self.assertIn(key, self.cases, 'Research failure is not covered by the benchmark')
        return self.cases[key]

    def deliver(self, key):
        (self.root / 'result.json').write_text(json.dumps(GOOD[key]), encoding='utf-8')
        if key == 'research-scope-v1':
            (self.root / 'baskets.csv').write_text(CSV, encoding='utf-8')

    def grade(self, key):
        return regression.grade(None, self.case(key), 'Everything is excellent; all checks passed.', self.root)

    def test_independent_good_research_fixtures_pass_without_a_judge(self):
        for key in list(GOOD)[:3]:
            self.deliver(key)
            result = self.grade(key)
            self.assertTrue(result['passed'], (key, result))
            self.assertEqual(result['judge_usage']['calls'], 0)

    def test_each_wrong_fact_is_detected_despite_confident_prose(self):
        for key, values in GOOD.items():
            for field in values:
                self.deliver(key)
                bad = copy.deepcopy(values)
                bad[field] = 'unsupported'
                (self.root / 'result.json').write_text(json.dumps(bad), encoding='utf-8')
                result = self.grade(key)
                row = next(r for r in result['items'] if r['id'] == field)
                self.assertEqual(row['status'], 'failed', (key, field))

    def test_omitted_second_basket_and_wrong_spreadsheet_fail(self):
        self.deliver('research-scope-v1')
        bad = dict(GOOD['research-scope-v1'])
        bad.pop('carriers')
        (self.root / 'result.json').write_text(json.dumps(bad), encoding='utf-8')
        (self.root / 'baskets.csv').write_text(CSV.split('carriers,')[0], encoding='utf-8')
        result = self.grade('research-scope-v1')
        rows = {r['id']:r for r in result['items']}
        self.assertEqual(rows['carriers']['status'], 'unverified')
        self.assertEqual(rows['spreadsheet']['status'], 'failed')
        self.assertFalse(result['passed'])

    def test_missing_files_never_pass(self):
        for key in GOOD:
            self.assertFalse(self.grade(key)['passed'])

    def test_correct_facts_do_not_certify_a_useful_recommendation(self):
        self.deliver('research-decision-v1')
        result = self.grade('research-decision-v1')
        rows = {r['id']:r for r in result['items']}
        self.assertTrue(all(rows[k]['status'] == 'passed' for k in GOOD['research-decision-v1']))
        self.assertEqual(rows['decision_usefulness']['status'], 'unverified')
        self.assertFalse(result['passed'])

    def test_oracles_include_review_basis_and_scope_limits(self):
        for key in GOOD:
            case = self.case(key)
            self.assertTrue(case['oracle']['basis'])
            self.assertTrue(case['oracle']['reviewed_by'])
            self.assertIn('synthetic', case['request'].lower())
            regression.validate_case(case)

    def test_csv_checks_data_not_platform_line_endings_or_quoting(self):
        expected = [['basket', 'ticker'], ['towers', 'ALP']]
        check = dict(kind='csv', path='table.csv', expected=expected)
        for text in ['basket,ticker\ntowers,ALP\n', '"basket","ticker"\r\n"towers","ALP"\r\n']:
            (self.root / 'table.csv').write_bytes(text.encode('utf-8'))
            self.assertEqual(regression.artifact_check(check, self.root)['status'], 'passed')
        (self.root / 'table.csv').write_text('basket,ticker\ntowers,WRONG\n', encoding='utf-8')
        self.assertEqual(regression.artifact_check(check, self.root)['status'], 'failed')

    def test_plausible_but_unsupported_research_does_not_pass(self):
        mistakes = {
            'research-scope-v1': dict(carriers=['CRN','DEL','ELM']),
            'research-sources-v1': dict(revenue_m=125, source_id='filing-original', growth_pct=25),
            'research-unknowns-v1': dict(measured_cached=0, measured_output=90, quota_saved_pct=10, cache_hit_rate=0),
            'research-decision-v1': dict(deployment_choice='B')}
        for key, overrides in mistakes.items():
            self.deliver(key)
            bad = dict(GOOD[key], **overrides)
            (self.root / 'result.json').write_text(json.dumps(bad), encoding='utf-8')
            result = self.grade(key)
            rows = {r['id']:r for r in result['items']}
            self.assertTrue(all(rows[field]['status']=='failed' for field in overrides), (key, rows))

    def test_independent_review_runs_after_generation_not_inside_worker(self):
        from tabularium.records import background_runtime as rt
        from agent_harness import put
        from tabularium.core.reply_envelope import Reply
        case = self.case('research-decision-v1')
        store = rt.TaskStore(self.root)
        calls = []
        def starter(store, chat, case, attempt):
            ident = regression.start_rerun(store, chat, case, attempt)
            put(store, ident, 'running')
            folder = store.directory(ident)
            (folder / 'trial_output').mkdir(exist_ok=True)
            (folder / 'trial_output/result.json').write_text(json.dumps(GOOD['research-decision-v1']), encoding='utf-8')
            (folder / 'result.txt').write_text('Test representative reliability before deployment.', encoding='utf-8')
            state = store.finish(ident, Reply('Test before deployment.', task_outcome='completed'))
            self.assertEqual(state, 'finished', 'Generation completion must not require its own subjective review')
            cp = json.loads((folder / 'completion.json').read_text(encoding='utf-8'))
            self.assertEqual(cp['verdict'], 'recorded')
            self.assertFalse(cp['passed'])
            return ident
        def judge(prompt, schema):
            calls.append(prompt)
            return json.dumps({'items':[dict(id='decision_usefulness',met=True,reason='Test double: review handoff only')]})
        result = regression.run(self.root,store,1,[case],judge,starter=starter,waiter=lambda s,i:s.get(i)['status'],log=lambda *a:None)
        self.assertEqual(len(calls),1)
        self.assertTrue(result['cases'][0]['attempts'][0]['passed'])

    def test_unfinished_benchmark_generation_does_not_become_recorded_success(self):
        from tabularium.records import background_runtime as rt
        from agent_harness import put
        from tabularium.core.reply_envelope import Reply
        store=rt.TaskStore(self.root)
        ident=regression.start_rerun(store,1,self.case('research-decision-v1'),1)
        put(store,ident,'running')
        self.assertEqual(store.finish(ident,Reply('Cut off',complete=False,task_outcome='completed')), 'needs_attention')
