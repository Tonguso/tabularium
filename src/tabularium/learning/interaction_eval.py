"""Controlled interactive cases for regression.run; production agents in disposable processes.
The user/delegate are deterministic environment actors. Agent choices and provider calls are real.
Only observations collected after the child exits are exposed to the independent artifact grader.
"""
from tabularium.paths import child_env
from tabularium.paths import home as _home
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from types import SimpleNamespace
from tabularium.learning import regression
from tabularium.channels.owner_telegram import OWNER_CHAT_ID as CHAT, OWNER_USER_ID
ROOT = _home()
MAX_STEPS = 16
TIMEOUT = 600


def _case(ident, request, files, observed):
    checks = [(path, key, value) for path, fields in files.items() for key, value in fields.items()]
    checks += [('observations.json', key, value) for key, value in observed.items()]
    return dict(id=ident, version=3, task_type='interaction', request=request,
                oracle=dict(basis='Controlled user events and persisted production state, independently checked.',
                            reviewed_by='Codex; synthetic behavioral specification'),
                requirements=[dict(id=f'check-{n}', description=f'{path}: {key}',
                                   check=dict(kind='json',path=path,pointer='/'+key,expected=value))
                              for n,(path,key,value) in enumerate(checks,1)])


CASES = [
    _case('interaction-steer',
          'Count the towers: there are 4. First save towers.json as {"towers":4}, then save result.json as a flat object with the same towers key.',
          {'result.json':dict(towers=4,carriers=3)}, dict(event_delivered=True)),
    _case('interaction-decision',
          'First save receipt.json containing {"prepared": true}. Then ask me which currency to use, GBP or USD; '
          'do not choose for me. After I answer, save result.json with the currency. Do not repeat the completed receipt write.',
          {'result.json':dict(currency='GBP')}, dict(waited=True,restarted=True,answered=True,receipt_writes=1)),
    _case('interaction-feedback',
          'Save result.json containing {"note": "ready"}.',
          {'result.json':dict(note='revised')}, dict(event_delivered=True,warning_delivered=True,correct_sources=True,warning_saved=False)),
    _case('interaction-delegation',
          'Review the existing Vit request eval-delegate attached to this task. Item 1 already has valid proof. '
          'Fetch the result, inspect it, reject missing reasoning, request targeted rework, then fetch and assess the new delivery. '
          'Preserve valid proof for unchanged work. Write result.json with accepted true only after the whole agreement is satisfied.',
          {'result.json':dict(accepted=True)}, dict(rejected=True,reworked=True,accepted=True,proof_preserved=True)),
]


def cases(name):
    # Only Cato has the delegate tool. The common three cases exercise every role.
    return [c for c in CASES if name == 'cato' or c['id'] != 'interaction-delegation']


def read(path, default=None):
    return json.loads(Path(path).read_text(encoding='utf-8')) if Path(path).exists() else default


def save(path, value):
    from tabularium.core.agent_runtime import atomic_json
    atomic_json(path, value)


def prepare(folder, name, route):
    """An empty agent folder for the role: no live skill, memory, conversation, queue or credentials."""
    root = Path(folder)/name
    root.mkdir(parents=True)
    (root/"task_state/background/1").mkdir(parents=True)
    (root/'SKILL.md').write_text('You are an assistant completing a synthetic local task. Use the available tools.\n',encoding='utf-8')
    style = root/'skills/talk-like-a-human-being/runtime-core.md'
    style.parent.mkdir(parents=True)
    style.write_text('Answer briefly and accurately.\n',encoding='utf-8')
    save(root/'routing.json', dict(enabled=True,routes=[route]))
    return root


def load_runtime(root):
    """Build the role's agent at its isolated path, without starting polling."""
    root = Path(root)
    from tabularium.agents import build
    agent = build(root, root.name)
    agent.MAX_TOOL_ITERATIONS = MAX_STEPS
    agent._trial = {'max_steps':MAX_STEPS}  # existing bounded-session behavior, no automatic retry loop
    agent._tool_scope = {'read_file','write_file','task_checkpoint','verify_requirement','task_decision'}
    agent.send_message = lambda *args,**kwargs: None
    return agent


def incoming(agent, ident, words, status='running'):
    """Synthetic Telegram payload goes through the real inbox/follow-up path."""
    payload = dict(message_id=ident,date=int(time.time()),chat=dict(id=CHAT,type='private'),
                   **{'from':dict(id=OWNER_USER_ID,is_bot=False,first_name='Evaluation user')},text=words)
    db = sqlite3.connect(agent._inbox.database)
    try:
        db.execute('INSERT INTO jobs(id,chat,message,handler,payload,status,created) VALUES (?,?,?,?,?,?,?)',
                   (ident,CHAT,ident,'handle_message',json.dumps(payload),status,time.time()))
        db.commit()
    finally:
        db.close()
    return payload


def activate(store):
    store.move(1,'claim','starting')
    store.move(1,'run','running')


def _delegate_fixture(agent, output, phase):
    from tabularium.records.background_runtime import TaskStore
    from tabularium.core.roster import ROLES
    from tabularium.records import verification
    remote = TaskStore(agent.AGENT_DIR.parent/'vitruvius', ROLES['vitruvius'])
    store=agent._background
    if phase=='first':
        artifact = output/'evidence.txt'
        artifact.write_text('Measured count: 4',encoding='utf-8')
        items = [dict(item='Measured count',check='Contains measured count',critical=True,early=True,
                      verification=dict(kind='contains',paths=[str(artifact)],text='Measured count: 4')),
                 dict(item='Explain both benefit and limitation',check='Read both arguments',critical=True)]
        child = remote.start(CHAT,'Synthetic delegated evidence',completion_items=items)['id']
        db = sqlite3.connect(remote.database)
        try:
            db.execute('CREATE TABLE engineering_requests(request_id TEXT PRIMARY KEY, digest TEXT, task INTEGER, deadline REAL, created REAL)')
            db.execute('INSERT INTO engineering_requests VALUES(?,?,?,?,?)',('eval-delegate','fixture',child,9999999999,0))
            db.commit()
        finally:
            db.close()
        remote.move(child,'claim','starting')
        remote.move(child,'run','running')
        remote.move(child,'finish','finished')
        (remote.directory(child)/'result.txt').write_text('Measured count: 4. Benefit: wider coverage. No limitation supplied.',encoding='utf-8')
        store=agent._background
        store.record_submission('eval-delegate',1,'vit','research',1000,items)
        store.add_delegation(1,'vit','eval-delegate','review evidence','worker_judgment')
        store.mark_delegation(1,'eval-delegate','returned')
        verification.execute(agent,CHAT,1,1,'eval-delegate')
        shutil.copyfile(verification.receipt_path(remote,child,'',1),agent.AGENT_DIR.parent/'initial-proof.json')
    else:
        child=1
    proof=(agent.AGENT_DIR.parent/'initial-proof.json').read_bytes()
    prior=read(agent.AGENT_DIR.parent/'observations.json',{})
    events={key:prior.get(key,False) for key in ('rejected','reworked')}
    fetched=False
    def endpoint(argv):
        nonlocal fetched
        action=argv[4]          # argv: python -m module agent action ...
        if action=='rework':
            request=read(argv[-1])
            try:
                previous, critical, unmet=verification.acceptance(store,1,'eval-delegate')
                accepted=not unmet
            except ValueError:
                accepted=False
            # Targeted rework is itself a rejection. Requiring an extra accept(not_met) call rewards duplication.
            events['rejected']=fetched and not accepted
            events['reworked']=request.get('items')==[2]
            remote.control(CHAT,child,'resume',request.get('note') or 'Supply the missing limitation')
            remote.acknowledge([c['id'] for c in remote.pending_controls(child)])
            remote.move(child,'claim','starting'); remote.move(child,'run','running')
            (remote.directory(child)/'result.txt').write_text(
                'Measured count: 4. Benefit: wider coverage. Limitation: the small sample may not generalize.',encoding='utf-8')
            remote.move(child,'finish','finished')
            result=dict(status='queued')
        elif action=='result':
            fetched=True
            result=dict(status='completed',text=(remote.directory(child)/'result.txt').read_text(encoding='utf-8'))
        else:
            raise ValueError('Only result and targeted rework are simulated.')
        return SimpleNamespace(returncode=0,stdout=json.dumps(result),stderr='')
    tool=agent.tool_delegate
    agent.tool_delegate=lambda **kw: tool(**kw,_runner=endpoint)
    agent._tool_scope.add('delegate')
    def observations():
        try:
            record, critical, unmet=verification.acceptance(store,1,'eval-delegate')
            accepted=not unmet
        except ValueError:
            accepted=False
        return dict(**events,accepted=accepted,
                    proof_preserved=verification.receipt_path(remote,child,'',1).read_bytes()==proof)
    return observations


def worker(root, case_id, phase):
    from tabularium.learning import trials
    from tabularium.records import task_decisions
    from tabularium.learning import corrections
    from tabularium.core.outcome import Refused
    from telebot.types import Message
    root=Path(root)
    case=next(c for c in CASES if c['id']==case_id)
    output=root/'outputs'
    output.mkdir(exist_ok=True)
    os.environ[trials.TRIAL_DIR_ENV]=str(output)
    os.environ[trials.READS_ENV]='[]'
    agent=load_runtime(root)
    store=agent._background
    feedback=case_id=='interaction-feedback'
    if feedback:
        agent.BACKGROUND_TASK_ID=''
    agent._inbox.local.job=101
    agent._inbox.local.message_id=101
    prompt=case['request']+'\nSave all requested files in '+str(output)+'.'
    if not feedback:
        prompt+=' Current background task: 1.'
    if phase=='first':
        store.start(CHAT,prompt,completion_items=[dict(item='Deliver the requested files and handle later instructions',
                                                       check='Read final files against the complete conversation',critical=True)])
        activate(store)
        payload=incoming(agent,101,prompt)
        agent._inbox.local.turn_messages=[(101,prompt)] if feedback else []
        messages=[dict(role='user',content=agent.compose_turn(Message.de_json(payload),nudge=False) if feedback else prompt)]
    else:
        messages=agent.load_conversation(CHAT)
        agent.conversations[CHAT]=messages
        if case_id=='interaction-decision':
            answer='Use GBP.'
            incoming(agent,201,answer)
            agent._inbox.local.message_id=201
            decisions=store.decisions(CHAT,1)
            if len(decisions)!=1:
                raise ValueError('Expected exactly one durable question after restart.')
            task_decisions.answer_from_current(agent,CHAT,decisions[0]['id'],answer,answer)
        else:
            from tabularium.records.background_runtime import TaskStore
            from tabularium.core.roster import ROLES
            remote=TaskStore(root.parent/'vitruvius',ROLES['vitruvius'])
            store.status_checker=lambda a,r:'completed' if remote.get(1)['status']=='finished' else 'running'
            store.poll_waiting()
        activate(store)
        agent._background_boundary(messages,[],CHAT)
    agent.conversations[CHAT]=messages
    observed=read(root.parent/'observations.json',{})
    writes=[]
    triggered=False
    delegate_observations=_delegate_fixture(agent,output,phase) if case_id=='interaction-delegation' else None
    original=agent.execute_tool
    def execute(name, inputs, chat_id=None):
        nonlocal triggered
        # Restrict delegate actions at the simulation boundary; never call a live delegate or create work.
        if name=='delegate' and (inputs.get('action') not in ('result','accept','rework') or inputs.get('request_id')!='eval-delegate'):
            return Refused('This environment contains only the existing eval-delegate request.')
        if name=='verify_requirement':
            from tabularium.records import verification
            requirements=verification.saved_items(store,int(inputs.get('task_id') or 1),inputs.get('request_id',''))
            number=inputs.get('item',0)
            if type(number) is int and 1 <= number <= len(requirements):
                paths=(requirements[number-1].get('verification') or {}).get('paths',[])
                if any(trials.read_refusal(path) for path in paths):
                    return Refused("Evaluation checks can read only this attempt's outputs.")
        result=original(name,inputs,chat_id)
        if name=='write_file' and not agent.unproductive(result):
            path=Path(inputs.get('path','')).resolve()
            writes.append(path.name)
            if not triggered and ((case_id=='interaction-steer' and path==output/'towers.json') or
                                  (feedback and path==output/'result.json')):
                triggered=True
                if feedback:
                    words='The note is wrong: it should say revised, not ready.'
                    incoming(agent,102,words,'pending')
                else:
                    store.control(CHAT,1,'steer','Also add the top-level key carriers with value 3 to result.json, preserving the towers key and total.')
        return result
    agent.execute_tool=execute
    boundary=agent._background_boundary
    def at_boundary(history, wire, chat):
        fresh=boundary(history, wire, chat)
        if feedback and fresh:
            from tabularium.core.reply_envelope import retry_note
            note=retry_note(agent,'evaluation runtime warning','', 'The previous action requires review.')
            wire.append(note)
            fresh.append(note)
            observed['warning_delivered']=True
        return fresh
    agent._background_boundary=at_boundary
    result=agent.run_turn(messages,CHAT)
    # A follow-up arriving during the final tool may become the next foreground turn.
    # Drain that queued user event too; a workflow test must not stop at the first reply.
    if feedback and agent._inbox.follow_ups(CHAT,101):
        agent._background_boundary(messages,[],CHAT)
        words,notes=agent._turn_notes.get(CHAT,('',''))
        agent._turn_notes[CHAT]=(words,notes+'\nRuntime warning: the previous action requires review.\n')
        result=agent.run_turn(messages,CHAT)
    agent.save_conversation(CHAT)
    status=store.finish(1,result) if not feedback else ('finished' if result.complete else 'needs_attention')
    if case_id=='interaction-steer':
        observed['event_delivered']=triggered and not store.pending_controls(1)
    elif case_id=='interaction-decision':
        observed['receipt_writes']=observed.get('receipt_writes',0)+writes.count('receipt.json')
        if phase=='first':
            observed.update(waited=status=='needs_answer' and len(store.decisions(CHAT,1))==1,first_pid=os.getpid())
        else:
            observed.update(restarted=observed.get('first_pid')!=os.getpid(),answered=not store.decisions(CHAT,1) and not store.pending_controls(1))
    elif feedback:
        records=corrections._pending(agent).get(str(CHAT),[])
        expected=[] if root.name=='tacitus' else [102]  # this role intentionally disables correction learning
        observed.update(event_delivered=triggered and 102 in [i for i,t in agent._inbox.turn_messages()],
                        correct_sources=[r.get('message_id') for r in records]==expected,
                        warning_saved='previous action requires review' in json.dumps(messages).lower())
    else:
        observed.update(delegate_observations())
    save(root.parent/'observations.json',observed)
    save(root.parent/('phase-'+phase+'.json'),dict(status=status,pid=os.getpid(),reply=str(result)))


def execution(route):
    return dict(profile='interactions',route=route,max_steps=MAX_STEPS,timeout=TIMEOUT,
                harness_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())


def executor(name, route, destination):
    """Single route, explicit timeout, no billing fallback or live background queue."""
    def execute(case, attempt):
        folder=Path(destination)/('interaction-'+uuid.uuid4().hex)
        root=prepare(folder,name,route)
        env=child_env(PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8',
                 TELEGRAM_BOT_TOKEN='1:evaluation',CALLIMACHUS_TELEGRAM_BOT_TOKEN='2:evaluation',
                 VITRUVIUS_TELEGRAM_BOT_TOKEN='3:evaluation',TACITUS_TELEGRAM_BOT_TOKEN='4:evaluation')
        env[name.upper()+'_BACKGROUND_TASK']='1'
        env.setdefault('ANTHROPIC_API_KEY','evaluation-unused')
        status='crashed'
        for phase in ('first','resume') if case['id'] in ('interaction-decision','interaction-delegation') else ('first',):
            log=open(folder/(phase+'.log'),'w',encoding='utf-8')
            try:
                proc=subprocess.Popen([sys.executable,'-B','-X','utf8','-m','tabularium.learning.interaction_eval',
                                     str(root),case['id'],phase],cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT,
                                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                proc.wait(timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                # Kill this isolated attempt's descendants before collecting observations.
                from tabularium.core.engine import AgentRuntime
                AgentRuntime._kill_tree(None,proc.pid)
                proc.wait(timeout=20)
                status='timeout'
                break
            finally:
                log.close()
            result=read(folder/('phase-'+phase+'.json'),{})
            status=result.get('status','crashed') if proc.returncode==0 else 'crashed'
            expected_wait={'interaction-decision':'needs_answer','interaction-delegation':'waiting_on_delegate'}.get(case['id'])
            if phase=='first' and status!=expected_wait:
                break
        output=folder/'trial_output'
        output.mkdir()
        for path in (root/'outputs').glob('*'):
            if path.is_file():
                shutil.copyfile(path,output/path.name)
        # Model-created observations are never trusted. Replace them after the process has exited.
        (output/'observations.json').unlink(missing_ok=True)
        if (folder/'observations.json').exists():
            shutil.copyfile(folder/'observations.json',output/'observations.json')
        usage=regression.generation_usage(root/'task_state/background/1')
        return dict(task=str(folder),status=status,folder=folder,generation_usage=usage)
    return execute


if __name__=='__main__':
    worker(Path(sys.argv[1]),sys.argv[2],sys.argv[3])
