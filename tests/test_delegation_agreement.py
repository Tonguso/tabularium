"""Real caller/delegate stores: one agreement, delivery-bound review, independent proof."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from tests.agent_harness import make_agent, put
from background_runtime import TaskStore
from agent_core import verification, task_decisions
from agent_core.roster import ROLES


def bind_delegate(store, task, request, items, agent='vit'):
    name, table = ('vitruvius', 'engineering_requests') if agent == 'vit' else ('callimachus', 'research_requests')
    remote = TaskStore(store.agent_dir.parent / name, ROLES[name])
    child = remote.start(store.get(task)['chat'], 'Synthetic delegated brief', completion_items=items)['id']
    db = sqlite3.connect(remote.database)
    try:
        db.execute(f'CREATE TABLE IF NOT EXISTS {table}(request_id TEXT PRIMARY KEY, digest TEXT, task INTEGER, deadline REAL, created REAL)')
        db.execute(f'INSERT INTO {table} VALUES(?,?,?,?,?)', (request, 'test', child, 9999999999, 0))
        db.commit()
    finally:
        db.close()
    put(remote, child, 'finished')
    (remote.directory(child)/'result.txt').write_text('DONE: delivered result', encoding='utf-8')
    return remote, child


class DelegationAgreement(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)/'cato'
        self.root.mkdir()
        self.agent = make_agent('cato', self.root)
        self.store = self.agent._background
        self.task = self.store.start(1, 'Owner brief', completion_items=[dict(item='Synthesis', check='Read it', critical=True)])['id']
        self.items = [dict(item='Explain tradeoffs', check='Assess reasoning', critical=True, early=True)]
        self.remote, self.child = bind_delegate(self.store, self.task, 'request', self.items)
        self.store.record_submission('request', self.task, 'vit', 'research', 1000, self.items)
        self.store.add_delegation(self.task, 'vit', 'request', 'research', 'worker_judgment')
        self.store.mark_delegation(self.task, 'request', 'returned')
        import delegation_contract
        p = patch.object(delegation_contract, 'BRIEFS', self.root/'briefs')
        p.start()
        self.addCleanup(p.stop)

    def fetch(self):
        text = (self.remote.directory(self.child)/'result.txt').read_text(encoding='utf-8')
        self.delivery_id = self.store.save_delegate_result(self.task, 'request', text)

    def accept(self, verdict='met', how='judgment'):
        return self.agent.tool_delegate(action='accept', chat_id=1, agent='vit', task_id=self.task,
            request_id='request', delivery_id=getattr(self, 'delivery_id', ''), verdicts=[dict(item=1, verdict=verdict, how=how,
            evidence='Read the delivered tradeoff and assessed the reasons against the brief.')],
            brief_lesson='The brief must identify the missing tradeoff and ask for evidence.')

    def completion(self):
        return task_decisions.completion_evidence(self.store, self.task,
            SimpleNamespace(task_outcome='completed', complete=True, question=''), {}, {})

    def test_inflight_cannot_be_accepted(self):
        self.fetch()
        put(self.remote, self.child, 'running')
        self.store.reopen_delegation(self.task, 'request')
        self.assertNotIn('"accepted": true', self.accept())

    def test_result_must_be_fetched(self):
        self.assertNotIn('"accepted": true', self.accept())

    def test_subjective_review_is_accepted_without_claiming_machine_proof(self):
        self.fetch()
        result = json.loads(self.accept(how='read'))
        self.assertTrue(result['accepted'])
        self.assertFalse(result['verified'])
        self.assertEqual(result['critical_unmet'], [])
        self.assertTrue(self.completion()['passed'])

    def test_explicit_rejection_cannot_disappear(self):
        self.fetch()
        result = json.loads(self.accept('not_met', 'judgment'))
        self.assertFalse(result['accepted'])
        self.assertFalse(self.completion()['passed'])
        self.assertEqual(json.loads(self.store.scorecard('request')['verdicts'])[0]['verdict'], 'not_met')

    def test_later_instruction_is_in_the_same_agreement(self):
        self.fetch()
        self.accept()
        self.remote.control(1, self.child, 'steer', 'Also explain uncertainty')
        control = self.remote.pending_controls(self.child)[0]['id']
        self.remote.acknowledge([control])
        verification.declare(self.remote, self.child, [dict(item='Explain uncertainty', check='Read uncertainty', critical=True, source=control)])
        self.assertEqual([i['item'] for i in self.store.contract_items('request')], ['Explain tradeoffs', 'Explain uncertainty'])
        self.assertFalse(self.completion()['passed'])

    def test_changed_result_invalidates_acceptance(self):
        self.fetch()
        self.accept()
        (self.remote.directory(self.child)/'result.txt').write_text('Different delivery', encoding='utf-8')
        self.assertFalse(self.completion()['passed'])

    def test_direct_rework_invalidates_owner_acceptance(self):
        self.fetch()
        self.accept()
        self.remote.control(1, self.child, 'resume', 'Fix the conclusion')
        self.assertFalse(self.completion()['passed'])

    def test_owner_checks_are_shared_with_delegate_and_reusable(self):
        artifact = self.root/'result.txt'
        artifact.write_text('tradeoff', encoding='utf-8')
        updated = dict(self.items[0], verification=dict(kind='contains', paths=[str(artifact)], text='tradeoff'))
        verification.declare(self.remote, self.child, [updated])
        self.fetch()
        check = json.loads(self.agent._execute_tool_unguarded('verify_requirement', dict(task_id=self.task, request_id='request', item=1), 1))
        self.assertEqual(check['status'], 'passed')
        self.assertEqual(verification.status(self.remote, self.child, '', 1), 'passed')
        self.remote.control(1, self.child, 'resume', 'Improve wording only')
        self.assertEqual(verification.status(self.remote, self.child, '', 1), 'passed')

    def test_stale_delivery_id_cannot_accept_another_sessions_fetch(self):
        self.fetch()
        old_id = self.delivery_id
        (self.remote.directory(self.child)/'result.txt').write_text('New result', encoding='utf-8')
        self.fetch()
        self.delivery_id = old_id
        self.assertNotIn('"accepted": true', self.accept())

    def test_missing_noncritical_review_and_invalid_provenance_are_refused(self):
        self.fetch()
        verification.declare(self.remote, self.child, [dict(item='Explain uncertainty', check='Read it', critical=False)])
        self.fetch()
        self.assertNotIn('"accepted": true', self.accept())
        self.assertNotIn('"accepted": true', self.accept(how='made-up'))

    def test_completed_owner_cannot_ignore_a_negative_review(self):
        from agent_core.reply_envelope import Reply
        self.fetch()
        self.accept('not_met')
        self.assertEqual(self.store.finish(self.task, Reply('All done', task_outcome='completed')), 'needs_attention')
        put(self.store, self.task, 'running')
        self.accept('met')
        self.assertEqual(self.store.finish(self.task, Reply('All done', task_outcome='completed')), 'finished')

    def test_legacy_owner_proof_migrates_without_running_the_check_again(self):
        import hashlib
        from agent_runtime import atomic_json
        artifact = self.root/'legacy.txt'
        artifact.write_text('correct', encoding='utf-8')
        item = dict(self.items[0], verification=dict(kind='contains', paths=[str(artifact)], text='correct'))
        verification.declare(self.remote, self.child, [item])
        db = sqlite3.connect(self.store.database)
        try:
            db.execute('INSERT OR REPLACE INTO requirements VALUES(?,?,?)', (self.task,'request',json.dumps([item])))
            db.commit()
        finally:
            db.close()
        path = self.store.directory(self.task)/'verification'/hashlib.sha256(b'request').hexdigest()[:24]/'1.json'
        record = dict(task=self.task, request='request', item=1, status='passed',
            requirement=hashlib.sha256(json.dumps([item, 'Owner brief'], sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            hashes={str(artifact.resolve()):hashlib.sha256(b'correct').hexdigest()})
        atomic_json(path, record)
        self.fetch()
        checked = json.loads(self.agent._execute_tool_unguarded('verify_requirement',
            dict(task_id=self.task, request_id='request', item=1), 1))
        self.assertTrue(checked['reused'])
        self.assertEqual(verification.status(self.remote, self.child, '', 1), 'passed')
        artifact.write_text('changed', encoding='utf-8')
        self.assertEqual(verification.status(self.store,self.task,'request',1),'unverified')

    def test_real_clients_share_answers_rescope_and_rework_for_both_delegates(self):
        for short, name, filename in [('vit','vitruvius','work_client.py'),('cal','callimachus','research_client.py')]:
            spec = importlib.util.spec_from_file_location('synthetic_client_'+short, Path(__file__).resolve().parents[1]/name/filename)
            client = importlib.util.module_from_spec(spec)
            if short == 'cal':
                from callimachus import knowledge
                loader_patch = patch.dict('sys.modules', knowledge=knowledge)
                loader_patch.start()
                self.addCleanup(loader_patch.stop)
            spec.loader.exec_module(client)
            client.ROOT = self.root.parent/name
            client.OWNER_CHAT_ID = 1
            request_id = 'roundtrip-'+short
            def runner(argv):
                request = json.loads(Path(argv[-1]).read_text()) if '--request-file' in argv else None
                response = client.call(argv[3], request=request, request_id=None if request else argv[-1])
                return SimpleNamespace(returncode=0,stdout=json.dumps(response),stderr='')
            import delegation_contract
            patcher = patch.object(delegation_contract,'CARDS',self.root/'cards')
            patcher.start()
            try:
                response=json.loads(self.agent.tool_delegate(action='submit',agent=short,chat_id=1,task_id=self.task,
                    request_id=request_id,brief='Explain the tradeoff from supplied evidence',purpose='Synthetic comparison',
                    completion_condition='Tradeoff explained',completion_items=self.items,budget_tokens=1000,
                    allowed_paths=[str(client.ROOT)],_runner=runner))
                remote=TaskStore(client.ROOT,ROLES[name]); child=response['task_id']
                remote.ask(1,child,'Include uncertainty?')
                self.agent.tool_delegate(action='answer',agent=short,chat_id=1,task_id=self.task,
                    request_id=request_id,note='Yes, explain uncertainty',_runner=runner)
                control=remote.pending_controls(child)[-1]['id']
                remote.acknowledge([control])
                verification.declare(remote,child,[dict(item='Explain uncertainty',check='Read uncertainty',critical=True,source=control)])
                self.assertEqual(len(self.store.contract_items(request_id)),2)
                self.agent.tool_delegate(action='rescope',agent=short,chat_id=1,task_id=self.task,request_id=request_id,
                    completion_items=[dict(id=2,item='Explain uncertainty',check='Read limits and uncertainty',critical=True,source=control)],_runner=runner)
                self.assertEqual(len(verification.saved_items(remote,child)),2)
                self.assertEqual(self.store.contract_items(request_id)[1]['check'],'Read limits and uncertainty')
                remote.acknowledge([r['id'] for r in remote.pending_controls(child)])
                put(remote,child,'finished')
                (remote.directory(child)/'result.txt').write_text('Tradeoff and uncertainty explained',encoding='utf-8')
                fetched=json.loads(self.agent.tool_delegate(action='result',agent=short,chat_id=1,task_id=self.task,
                    request_id=request_id,_runner=runner))
                reviewed=json.loads(self.agent.tool_delegate(action='accept',agent=short,chat_id=1,task_id=self.task,
                    request_id=request_id,delivery_id=fetched['delivery_id'],verdicts=[dict(item=n,verdict='met',how='judgment',
                    evidence='Read the actual tradeoff and uncertainty; both address the brief.') for n in (1,2)]))
                self.assertTrue(reviewed['accepted'])
                self.agent.tool_delegate(action='rework',agent=short,chat_id=1,task_id=self.task,request_id=request_id,
                    items=[2],note='Clarify uncertainty only',_runner=runner)
                self.assertTrue(self.store.unread_delegations(self.task))
                self.assertEqual(len(self.store.contract_items(request_id)),2)
            finally:
                patcher.stop()

    def test_result_remains_readable_when_a_receipt_is_missing(self):
        artifact=self.root/'checked.txt'
        artifact.write_text('tradeoff',encoding='utf-8')
        verification.declare(self.remote,self.child,[dict(self.items[0],verification=dict(kind='contains',paths=[str(artifact)],text='tradeoff'))])
        self.fetch()
        self.agent._execute_tool_unguarded('verify_requirement',dict(task_id=self.task,request_id='request',item=1),1)
        self.accept(how='ran')
        verification.receipt_path(self.remote,self.child,'',1).unlink()
        self.fetch()
        self.assertNotIn('"accepted": true',self.accept(how='ran'))


if __name__ == '__main__':
    unittest.main()
