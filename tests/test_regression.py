"""Independent oracle, complete coverage, actual outputs, honest token accounting."""
import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from agent_core import regression, trials
from agent_harness import put, make_agent, AGENTS
import background_runtime as rt


def subjective():
    return dict(id='rubric',version=1,request='Do A and B',oracle=dict(basis='Independent rubric',reviewed_by='test'),
                requirements=[dict(id=n,check=dict(kind='judgment',rubric='Must do '+n)) for n in ('A','B')])


class Evaluation(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.case=regression.load_cases(self.root)[0]

    def deliver(self, values=None):
        if values is None:
            values=dict(gross_pence=4550,refund_pence=750,net_pence=3800,currency='GBP')
        (self.root/'result.json').write_text(json.dumps(values),encoding='utf-8')

    def test_subjective_coverage_must_be_exact_and_typed(self):
        valid=[dict(id='A',met=True,reason='A present'),dict(id='B',met=True,reason='B present')]
        for rows in (valid[:1], valid+[valid[0]], [valid[0],dict(id='C',met=True,reason='extra')],
                     [valid[0],dict(id='B',met='true',reason='bad type')], []):
            result=regression.grade(lambda p,s:json.dumps(dict(items=rows)),subjective(),'Candidate')
            self.assertFalse(result['passed'])
            self.assertEqual([r['id'] for r in result['items']],['A','B'])
            self.assertTrue(all(r['status']=='unverified' for r in result['items']))
        self.assertTrue(regression.grade(lambda p,s:json.dumps(dict(items=valid)),subjective(),'Candidate')['passed'])

    def test_actual_deliverable_passes_without_a_model_judge(self):
        self.deliver()
        def never(*args): self.fail('Objective checks must not call a judge')
        result=regression.grade(never,self.case,'A modest reply',self.root)
        self.assertTrue(result['passed'])
        self.assertEqual(result['quality'],1)
        self.assertEqual(result['judge_usage']['input_tokens'],0)
        self.assertEqual(len(result['items']),4)
        self.assertEqual(len(result['items'][0]['evidence']['sha256']),64)

    def test_confident_reply_cannot_replace_missing_or_wrong_files(self):
        self.assertFalse(regression.grade(None,self.case,'All requirements passed!',self.root)['passed'])
        self.deliver(dict(gross_pence=4550,refund_pence=750,net_pence=9999,currency='GBP'))
        result=regression.grade(None,self.case,'Everything is correct',self.root)
        self.assertFalse(result['passed'])
        self.assertEqual(result['quality'],0.75)
        self.assertEqual(result['items'][2]['status'],'failed')

    def test_missing_null_field_is_not_an_explicit_unknown(self):
        case=regression.load_cases(self.root)[1]
        self.deliver(dict(time='15:00',source_id='note-2',booking_confirmed=False))
        result=regression.grade(None,case,'Done',self.root)
        self.assertEqual(result['items'][2]['status'],'unverified')
        self.assertFalse(result['passed'])

    def test_boolean_cannot_be_replaced_by_zero(self):
        case=regression.load_cases(self.root)[1]
        self.deliver(dict(time='15:00',source_id='note-2',location=None,booking_confirmed=0))
        self.assertFalse(regression.grade(None,case,'Done',self.root)['passed'])

    def test_empty_duplicate_untrusted_cases_are_rejected(self):
        for case in (dict(self.case,requirements=[]), dict(self.case,oracle={}),
                     dict(self.case,requirements=[self.case['requirements'][0]]*2)):
            self.assertRaises(ValueError,regression.validate_case,case)
        self.assertFalse(regression.grade(None,dict(request='r',plan=['a'],reference='old answer'),'new')['passed'])

    def test_no_silent_truncation_or_judge_crash(self):
        def broken(*args): raise RuntimeError('unavailable')
        self.assertFalse(regression.grade(broken,subjective(),'Candidate')['passed'])
        self.assertFalse(regression.grade(broken,subjective(),'x'*40001)['passed'])
        self.assertFalse(regression.grade(lambda p,s:'[]',subjective(),'Candidate')['passed'])

    def test_artifact_cannot_escape_output_directory(self):
        case=copy.deepcopy(self.case)
        case['requirements'][0]['check']['path']='../secret.json'
        self.assertRaises(ValueError,regression.validate_case,case)

    def test_workbook_checks_inspect_cells_not_answer_claims(self):
        from openpyxl import Workbook
        book=Workbook();book.active['A1']=123;book.save(self.root/'output.xlsx');book.close()
        case=dict(id='sheet',version=1,request='Produce 123',oracle=self.case['oracle'],requirements=[
            dict(id='cell',check=dict(kind='xlsx',path='output.xlsx',sheet='Sheet',cell='A1',expected=124))])
        self.assertFalse(regression.grade(None,case,'Cell is 124',self.root)['passed'])
        case['requirements'][0]['check']['expected']=123
        self.assertTrue(regression.grade(None,case,'',self.root)['passed'])

    def test_corrupt_workbook_is_unverified_without_crashing(self):
        (self.root/'broken.xlsx').write_bytes(b'not a workbook')
        case=dict(id='sheet',version=1,request='Produce 123',oracle=self.case['oracle'],requirements=[
            dict(id='cell',check=dict(kind='xlsx',path='broken.xlsx',sheet='Sheet',cell='A1',expected=123))])
        result=regression.grade(None,case,'It is correct',self.root)
        self.assertFalse(result['passed'])
        self.assertEqual(result['items'][0]['status'],'unverified')

    def test_comparison_keeps_unknown_usage_unknown(self):
        before=dict(benchmark='same',k=1,quality=0.5,pass_1=1,generation_usage=dict(known=False),
                    judge_usage=dict(known=True,input_tokens=100,output_tokens=20))
        after=dict(before,quality=1,pass_1=2,judge_usage=dict(known=True,input_tokens=0,output_tokens=0))
        result=regression.compare(before,after)
        self.assertEqual(result['quality_delta'],0.5)
        self.assertIsNone(result['generation_usage_delta'])
        self.assertEqual(result['judge_usage_delta'],dict(input_tokens=-100,output_tokens=-20))
        self.assertFalse(regression.compare(before,dict(after,benchmark='changed'))['comparable'])

    def test_every_role_gets_same_benchmark_and_scope(self):
        for name in AGENTS:
            root=self.root/name;root.mkdir()
            agent=make_agent(name,root)
            trials.scope(agent,dict(eval=dict(benchmark='test')))
            self.assertIn('write_file',agent._tool_scope)
            self.assertNotIn('execute_command',agent._tool_scope)
            self.assertNotIn('remember',agent._tool_scope)
            self.assertEqual(regression.load_cases(root),regression.load_cases(self.root))

    def test_collect_never_promotes_history_to_trusted_cases(self):
        store=rt.TaskStore(self.root)
        row=store.start(1,'Past work')
        folder=store.directory(row['id'])
        (folder/'checkpoint.json').write_text('{"plan":["A"]}')
        (folder/'result.txt').write_text('Agent said it was correct. '*20)
        put(store,row['id'],'finished')
        cases=regression.collect(self.root,store)
        self.assertFalse(cases[0]['trusted'])
        self.assertRaises(ValueError,regression.validate_case,cases[0])
        self.assertRaises(ValueError,regression.collect,self.root/'tacitus',store)

    def test_run_measures_quality_usage_and_compares_only_same_benchmark(self):
        store=rt.TaskStore(self.root)
        values=iter([3800,9999,3800])
        def starter(store,chat,case,n):
            ident=regression.start_rerun(store,chat,case,n)
            folder=store.directory(ident)
            context=json.loads((folder/'context.json').read_text())
            self.assertFalse(context['notify_telegram'])
            self.assertNotIn('oracle',json.dumps(context))
            out=folder/'trial_output';out.mkdir()
            (out/'result.json').write_text(json.dumps(dict(gross_pence=4550,refund_pence=750,net_pence=next(values),currency='GBP')))
            (folder/'result.txt').write_text('All done')
            usage=folder/'task_state';usage.mkdir()
            db=sqlite3.connect(usage/'inbox.sqlite3')
            try:
                db.execute('CREATE TABLE usage_events(id INTEGER,created REAL,kind TEXT,metrics TEXT,provider TEXT,model TEXT,billing TEXT)')
                db.execute('INSERT INTO usage_events VALUES(1,0,?,?,?,?,?)',('response',json.dumps(dict(input_total=100,cache_read=20,output=10)),'test','model','test'))
                db.commit()
            finally: db.close()
            put(store,ident,'finished')
            return ident
        run=lambda cases,k:regression.run(self.root,store,1,cases,None,k=k,starter=starter,waiter=lambda s,i:'finished',log=lambda *a:None)
        record=run([self.case],2)
        self.assertEqual((record['pass_1'],record['pass_k'],record['flaky']),(0.5,0,1))
        self.assertEqual(record['quality'],0.875)
        self.assertEqual(record['generation_usage']['input_total'],200)
        self.assertEqual(record['generation_usage']['output'],20)
        self.assertTrue(record['generation_usage']['known'])
        self.assertEqual(record['judge_usage']['calls'],0)
        changed=dict(self.case,version=2)
        run([changed],1)
        self.assertIsNone(regression.latest(self.root)['previous_pass_1'])
        self.assertFalse(regression.generation_usage(self.root/'missing')['known'])


if __name__=='__main__':
    unittest.main()
