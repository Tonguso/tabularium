"""Interactive evaluator: real runtime boundaries, independent observations, no live agent state."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from agent_core import regression


class InteractiveEvaluation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_workflows_are_explicitly_selected_and_not_lesson_trial_inputs(self):
        from agent_core import interaction_eval as ie
        ordinary = regression.load_cases(self.root)
        cases = ie.cases('cato')
        self.assertEqual(len(cases), 4)
        self.assertFalse({c['id'] for c in ordinary} & {c['id'] for c in cases})
        for case in cases:
            regression.validate_case(case)
            self.assertTrue(any(r['check']['path'] == 'observations.json' for r in case['requirements']))

    def test_missing_event_cannot_pass_even_with_correct_deliverable(self):
        from agent_core import interaction_eval as ie
        case = ie.cases('cato')[0]
        (self.root/'result.json').write_text(json.dumps({'towers': 4, 'carriers': 3}), encoding='utf-8')
        result = regression.grade(None, case, 'All done', self.root)
        self.assertFalse(result['passed'])
        self.assertLess(result['quality'], 1)

    def test_crashed_executor_cannot_pass_on_leftover_files(self):
        from agent_core import interaction_eval as ie
        case = ie.cases('cato')[0]
        out = self.root/'attempt'/'trial_output'
        out.mkdir(parents=True)
        for requirement in case['requirements']:
            check = requirement['check']
            path = out/check['path']
            data = json.loads(path.read_text()) if path.exists() else {}
            data[check['pointer'][1:]] = check['expected']
            path.write_text(json.dumps(data), encoding='utf-8')
        def execute(case, attempt):
            return dict(task=1, status='crashed', folder=out.parent)
        record = regression.run(self.root, None, 1, [case], None, executor=execute, log=lambda s:None)
        self.assertEqual(record['quality'], 0)
        self.assertFalse(record['cases'][0]['attempts'][0]['passed'])
        self.assertFalse(record['generation_usage']['known'])

    def test_comparison_refuses_different_model_routes(self):
        usage = dict(known=True, accounting_version=2, input_total=100,cache_read=0,output=10,estimated_effective_tokens=110)
        before = dict(benchmark='same',k=1,quality=1,pass_1=1,generation_usage=usage,
                      execution={'profile':'interactions','route':{'kind':'anthropic_subscription','model':'claude-a'}})
        after = copy.deepcopy(before)
        after['execution']['route']['model']='claude-b'
        self.assertFalse(regression.compare(before,after)['comparable'])



class RuntimeScenarios(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)
        env={'ANTHROPIC_API_KEY':'test','TELEGRAM_BOT_TOKEN':'1:a','CALLIMACHUS_TELEGRAM_BOT_TOKEN':'2:b',
             'VITRUVIUS_TELEGRAM_BOT_TOKEN':'3:c','TACITUS_TELEGRAM_BOT_TOKEN':'4:d',
             **{n.upper()+'_BACKGROUND_TASK':'1' for n in ('cato','callimachus','vitruvius','tacitus')}}
        p=patch.dict('os.environ',env)
        p.start();self.addCleanup(p.stop)

    def test_real_state_boundaries_for_every_role(self):
        from unittest.mock import patch
        from agent_core import interaction_eval as ie, reply_envelope
        import provider_routing
        for name in ('cato','callimachus','vitruvius','tacitus'):
            for case in ie.cases(name):
                root=ie.prepare(self.base/(name+case['id']),name,{'kind':'anthropic_subscription','model':'claude-test'})
                calls=[]
                def model(agent,messages,chat):
                    calls.append(1)
                    output=root/'outputs'
                    def tool(name,**kw):
                        result=agent.execute_tool(name,kw,chat)
                        agent._background_boundary(messages,[],chat)
                        return result
                    def write(file,data):
                        return tool('write_file',path=str(output/file),content=json.dumps(data))
                    if case['id']=='interaction-steer':
                        write('towers.json',{'towers':4})
                        self.assertIn('carriers',json.dumps(messages))
                        write('result.json',{'towers':4,'carriers':3})
                    elif case['id']=='interaction-decision':
                        if len(calls)==1:
                            write('receipt.json',{'prepared':True})
                            tool('task_decision',action='ask',task_id=1,question='GBP or USD?')
                        else:
                            self.assertIn('Use GBP.',json.dumps(messages))
                            write('result.json',{'currency':'GBP'})
                    elif case['id']=='interaction-feedback':
                        write('result.json',{'note':'ready'})
                        self.assertIn('revised',json.dumps(messages))
                        write('result.json',{'note':'revised'})
                        return reply_envelope.Reply('Revised.',correction=[102],answers=[101,102],request='done')
                    else:
                        def delegate(action,**kw):
                            return tool('delegate',action=action,agent='vit',request_id='eval-delegate',**kw)
                        json.loads(delegate('result'))
                        delegate('rework',items=[2],note='Supply the missing limitation only.')
                        second=json.loads(delegate('result'))
                        delegate('accept',delivery_id=second['delivery_id'],verdicts=[
                            dict(item=1,verdict='met',how='read',evidence='The existing requirement-bound check passed.'),
                            dict(item=2,verdict='met',how='read',evidence='Small sample limits generalization')])
                        write('result.json',{'accepted':True})
                    return reply_envelope.Reply('Done.',task_outcome='completed',request='done')
                p=patch.object(provider_routing,'run',side_effect=model)
                p.start()
                try:
                    ie.worker(root,case['id'],'first')
                    if case['id']=='interaction-decision':
                        first=ie.read(root.parent/'observations.json')
                        self.assertTrue(first['waited'],(name,first))
                        # The subprocess integration test checks process identity; this test checks durable reload.
                        first['first_pid']=-1
                        ie.save(root.parent/'observations.json',first)
                        ie.worker(root,case['id'],'resume')
                finally:
                    p.stop()
                shutil = __import__('shutil')
                shutil.copyfile(root.parent/'observations.json',root/'outputs/observations.json')
                result=regression.grade(None,case,'',root/'outputs')
                self.assertTrue(result['passed'],(name,case['id'],result))

    def test_plausible_artifacts_do_not_hide_broken_interactions(self):
        from unittest.mock import patch
        from agent_core import interaction_eval as ie, reply_envelope
        import provider_routing
        for case in ie.cases('cato'):
            root=ie.prepare(self.base/case['id'],'cato',{'kind':'anthropic_subscription','model':'claude-test'})
            def model(agent,messages,chat):
                output=root/'outputs'
                def tool(name,**kw):
                    result=agent.execute_tool(name,kw,chat)
                    agent._background_boundary(messages,[],chat)
                    return result
                def write(data):
                    tool('write_file',path=str(output/'result.json'),content=json.dumps(data))
                if case['id']=='interaction-steer':
                    # Guessed final data; the later requirement was never delivered.
                    write({'towers':4,'carriers':3})
                elif case['id']=='interaction-decision':
                    # Guessed the currency without asking or surviving a restart.
                    write({'currency':'GBP'})
                elif case['id']=='interaction-feedback':
                    write({'note':'ready'})
                    write({'note':'revised'})
                    return reply_envelope.Reply('Revised',correction=[101],answers=[101,102],request='done')
                else:
                    def delegate(action,**kw):
                        return tool('delegate',action=action,agent='vit',request_id='eval-delegate',**kw)
                    def accept(delivery):
                        return delegate('accept',delivery_id=delivery['delivery_id'],verdicts=[
                            dict(item=1,verdict='met',how='read',evidence='The existing requirement-bound check passed.'),
                            dict(item=2,verdict='met',how='read',evidence='Unsupported claim that both arguments are present.')])
                    first=json.loads(delegate('result'))
                    accept(first)  # Incorrectly accept the missing argument, then repair the final output.
                    delegate('rework',items=[2],note='Add the missing limitation only.')
                    accept(json.loads(delegate('result')))
                    write({'accepted':True})
                return reply_envelope.Reply('Done',task_outcome='completed',request='done')
            p=patch.object(provider_routing,'run',side_effect=model)
            p.start()
            try:
                ie.worker(root,case['id'],'first')
            finally:
                p.stop()
            __import__('shutil').copyfile(root.parent/'observations.json',root/'outputs/observations.json')
            artifact_only=dict(case,requirements=[r for r in case['requirements'] if r['check']['path']!='observations.json'])
            old=regression.grade(None,artifact_only,'Done',root/'outputs')
            new=regression.grade(None,case,'Done',root/'outputs')
            self.assertTrue(old['passed'],(case['id'],old))
            self.assertFalse(new['passed'],(case['id'],new))
            self.assertEqual(new['judge_usage']['calls'],0)

    def test_isolated_tools_cannot_read_or_write_outside_outputs(self):
        from unittest.mock import patch
        from agent_core import interaction_eval as ie, reply_envelope
        import provider_routing
        secret=self.base/'private.txt'
        secret.write_text('Synthetic private sentinel',encoding='utf-8')
        target=self.base/'outside.txt'
        for name in ('cato','callimachus','vitruvius','tacitus'):
            root=ie.prepare(self.base/name,name,{'kind':'anthropic_subscription','model':'claude-test'})
            def model(agent,messages,chat):
                for tool,args in [('read_file',dict(path=str(secret))),('write_file',dict(path=str(target),content='changed')),
                                  ('execute_command',dict(command='echo unexpected')),('send_email',dict(to='fake@example.com'))]:
                    result=agent.execute_tool(tool,args,chat)
                    self.assertTrue(agent.unproductive(result),(name,tool,result))
                return reply_envelope.Reply('Stopped',complete=False)
            p=patch.object(provider_routing,'run',side_effect=model)
            p.start()
            try:
                ie.worker(root,'interaction-steer','first')
            finally:
                p.stop()
        self.assertFalse(target.exists())
        self.assertEqual(secret.read_text(),'Synthetic private sentinel')

    def test_followup_after_last_sdk_tool_becomes_next_turn(self):
        from unittest.mock import patch
        from agent_core import interaction_eval as ie, reply_envelope
        import provider_routing
        root=ie.prepare(self.base/'late','cato',{'kind':'anthropic_subscription','model':'claude-test'})
        calls=[]
        def model(agent,messages,chat):
            calls.append(1)
            agent._background_boundary(messages,[],chat)  # SDK boundary precedes execution
            if len(calls)>1:
                self.assertIn('revised',json.dumps(messages))
            agent.execute_tool('write_file',dict(path=str(root/'outputs/result.json'),
                               content=json.dumps({'note':'ready' if len(calls)==1 else 'revised'})),chat)
            return reply_envelope.Reply('Saved',request='done',correction=[] if len(calls)==1 else [102],
                                       answers=[101,102])
        p=patch.object(provider_routing,'run',side_effect=model)
        p.start()
        try:
            ie.worker(root,'interaction-feedback','first')
        finally:
            p.stop()
        self.assertEqual(len(calls),2)
        observed=ie.read(root.parent/'observations.json')
        self.assertTrue(observed['event_delivered'])
        self.assertTrue(observed['warning_delivered'])
        self.assertTrue(observed['correct_sources'])
        self.assertFalse(observed['warning_saved'])

    def test_delegate_parking_resumes_from_existing_delivery_and_proof(self):
        from unittest.mock import patch
        from agent_core import interaction_eval as ie, reply_envelope
        import provider_routing
        root=ie.prepare(self.base/'park','cato',{'kind':'anthropic_subscription','model':'claude-test'})
        calls=[]
        def model(agent,messages,chat):
            calls.append(1)
            def delegate(action,**kw):
                return agent.execute_tool('delegate',dict(action=action,agent='vit',request_id='eval-delegate',**kw),chat)
            delivery=json.loads(delegate('result'))
            if len(calls)==1:
                delegate('rework',items=[2],note='Add the missing limitation only.')
                return reply_envelope.Reply('Waiting for the delegate.',task_outcome='waiting')
            self.assertIn('returned',json.dumps(messages))
            delegate('accept',delivery_id=delivery['delivery_id'],verdicts=[
                dict(item=1,verdict='met',how='read',evidence='Existing unchanged requirement-bound proof passed.'),
                dict(item=2,verdict='met',how='read',evidence='The small sample limitation is now explained.')])
            agent.execute_tool('write_file',dict(path=str(root/'outputs/result.json'),content='{"accepted":true}'),chat)
            return reply_envelope.Reply('Done',task_outcome='completed')
        p=patch.object(provider_routing,'run',side_effect=model)
        p.start()
        try:
            ie.worker(root,'interaction-delegation','first')
            self.assertEqual(ie.read(root.parent/'phase-first.json')['status'],'waiting_on_delegate')
            ie.worker(root,'interaction-delegation','resume')
        finally:
            p.stop()
        observed=ie.read(root.parent/'observations.json')
        self.assertTrue(all(observed[k] for k in ('rejected','reworked','accepted','proof_preserved')),observed)
        self.assertEqual(ie.read(root.parent/'phase-resume.json')['status'],'finished')
