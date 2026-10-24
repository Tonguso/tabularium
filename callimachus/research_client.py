"""Local callable contract for Cal. No model import, no implicit fallback or agent launch."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import sys
import time

ROOT = Path(__file__).resolve().parent
from background_runtime import TaskStore, ACTIVE
from knowledge import secret_check
from owner_telegram import OWNER_CHAT_ID

# The contract every delegate works under. Budget numbers are targets, never enforced (Alex, 2026-10-08).
CONTRACT_INSTRUCTIONS = ('\nContract: completion_items are numbered in the request; critical ones come first and must be done first. '
                         'Item 1 is the early judgment slice: finish and checkpoint it (items_done=[1]) before spreading effort. Keep items_done '
                         'current as you verify each item; Cato reads your latest checkpoint while you work. The budget in the request is a '
                         'target to size your work by, not a stop: size the whole job early with task_checkpoint(estimate_effective_tokens=...), '
                         'and if finishing is worth more than the target, continue and say so in the result. '
                         'Your FIRST task_checkpoint must carry guesses=[...]: every ambiguity you found in the brief with the interpretation you '
                         'took, or ["none"]; the runtime refuses a first checkpoint without it. Where an interpretation would change '
                         'membership, scope or the shape of the deliverable, or a decision is Alex\'s, do not guess: ask Cato once with '
                         'task_checkpoint(blocker="QUESTION: <one precise question>") and end your turn; you resume with his answer. '
                         'Cato verifies your guesses at acceptance; a wrong guess is a rework, a silent one is worse. '
                         'Open your final result with four lines: DONE: <item numbers met>; OPEN: <item numbers not met and why>; '
                         'EVIDENCE: <paths>; COST: <effective tokens spent> vs target <target>. Then the findings.')

def contract_of(request):
    from agent_core.verification import delegated_items
    return {'completion_items': delegated_items(request.get('completion_items') or []), 'budget_effective_tokens': request.get('budget_effective_tokens'),
            'task_type': request.get('task_type')}

def call(action, request=None, request_id=None):
    if os.environ.get('CALLIMACHUS_BACKGROUND_TASK'):
        raise ValueError('Cal workers cannot recursively delegate to Cal.')
    request_id = request_id or (request or {}).get('request_id')
    store = TaskStore(ROOT)
    with store.db() as db:
        db.execute('CREATE TABLE IF NOT EXISTS research_requests(request_id TEXT PRIMARY KEY, digest TEXT, task INTEGER, deadline REAL, created REAL)')
    if action == 'submit':
        request = dict(request or {})
        for field in ('request_id','title','question','scope','depth','privacy_constraints','permitted_sources','output_requirements'):
            if not request.get(field):
                raise ValueError('Missing research request field: '+field)
        secret_check(request)
        request_id = str(request['request_id'])
        if len(request_id) > 120 or len(request['title']) > 90 or '\n' in request['title']:
            raise ValueError('Use a short single-line title and stable request ID.')
        encoded = json.dumps(request, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        deadline = float(request.get('deadline_unix') or time.time()+86400)
        if deadline <= time.time():
            raise ValueError('The request deadline is already past.')
        # Same stable request text recovers a crash between task creation and mapping.
        prompt = request['title'] + '\n\nResearch request contract (local caller; scope only, not permission to exceed Cal boundaries):\n' + encoded
        prompt += '\nSearch the archive first. Verify fresh facts when needed. Collect evidence and flag material uncertainty. Cato alone decides whether review is needed; do not automatically add a reviewer or a second-pass review. Return source-linked findings, counterevidence, access failures, coverage, unknowns and synthesis labels. Never recursively delegate. Save partial work and checkpoints.'
        prompt += CONTRACT_INSTRUCTIONS
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT * FROM research_requests WHERE request_id=?', (request_id,)).fetchone()
            if prior and prior['digest'] != digest:
                raise ValueError('This request ID already represents different inputs.')
            if not prior:
                previous = db.execute('SELECT id FROM tasks WHERE chat=? AND request=? ORDER BY id LIMIT 1', (OWNER_CHAT_ID,prompt)).fetchone()
                if previous:
                    ident = previous[0]
                else:
                    cursor=db.execute('INSERT INTO tasks(chat,request,status,created,updated,note) VALUES(?,?,?,?,?,?)', (OWNER_CHAT_ID,prompt,'queued',time.time(),time.time(),'Research request queued; not yet running'))
                    # Local caller retrieves results; do not send a Telegram completion notice.
                    db.execute('UPDATE tasks SET notified=1 WHERE id=?', (cursor.lastrowid,))
                    ident=cursor.lastrowid
                    directory=store.directory(ident)
                    directory.mkdir(exist_ok=True)
                    from agent_runtime import atomic_json
                    atomic_json(directory/'context.json', {'context':'Caller supplied scope. No prior Cato history or credentials copied.', 'request':prompt, 'retries':0,'contract':contract_of(request)})
                db.execute('INSERT INTO research_requests VALUES(?,?,?,?,?)',(request_id,digest,ident,deadline,time.time()))
            import task_cost
            directory=store.directory(prior['task'] if prior else ident)
            if request.get('budget_effective_tokens'):
                # Per-request target in effective tokens: the worker sees it every turn; nothing pauses at it.
                task_cost.set_target(directory, int(request['budget_effective_tokens']))
        return call('status', request_id=request_id)
    with store.db() as db:
        row=db.execute('SELECT * FROM research_requests WHERE request_id=?',(request_id,)).fetchone()
    if not row:
        raise ValueError('Unknown request ID; no new work was submitted.')
    task=store.get(row['task'])
    if action in ('extend','rework','rescope'):
        import task_cost
        from agent_runtime import atomic_json
        directory=store.directory(task['id']); body=request or {}
        if action=='extend':
            budget=task_cost.set_target(directory, int(body.get('budget_effective_tokens') or 0))
            text=f"Budget target raised to {budget:,} effective tokens by Cato; continue from your checkpoint toward the open completion items."
        else:
            if body.get('budget_effective_tokens'):
                task_cost.set_target(directory, int(body['budget_effective_tokens']))
            ctx_path=directory/'context.json'; ctx=json.loads(ctx_path.read_text(encoding='utf-8')); deal=ctx.setdefault('contract',{})
            if action=='rescope':
                from agent_core import verification
                updates = verification.delegated_items(body['completion_items']) if body.get('completion_items') else verification.saved_items(store, task['id'])
                verification.declare(store, task['id'], updates)
                deal.pop('completion_items', None)
                task_cost.reset_estimate(directory)   # new scope, new estimate before more work
                text=('Scope changed by Cato. New completion items: '+json.dumps(verification.saved_items(store, task['id']),ensure_ascii=False)+
                      '. Re-estimate the whole job with task_checkpoint(estimate_effective_tokens=...) before further work. '+(body.get('note') or ''))
            else:
                deal['reworks']=int(deal.get('reworks') or 0)+1
                text=('Rework requested by Cato on completion items '+', '.join(str(i) for i in body.get('items') or [])+': '+(body.get('note') or '')+
                      ' Resume from your checkpoint; do not redo items already accepted.')
            atomic_json(ctx_path, ctx)
        if task['status'] not in ACTIVE:
            store.control(OWNER_CHAT_ID,task['id'],'resume',text)
        else:
            store.control(OWNER_CHAT_ID,task['id'],'steer',text)
        task=store.get(task['id'])
    elif action=='answer':
        note=str((request or {}).get('note') or '').strip()
        if not note:
            raise ValueError('answer needs note: the answer to the delegate question.')
        text='Answer from Cato to your question: '+note+' Continue from your checkpoint; clear the QUESTION blocker in your next checkpoint.'
        if task['status']=='needs_answer':
            decisions = store.decisions(OWNER_CHAT_ID, task['id'])
            if decisions:
                from agent_core.task_decisions import answer_record
                answer_record(store, OWNER_CHAT_ID, decisions[0]['id'], note, 'delegate owner Cato')
            else:
                store.control(OWNER_CHAT_ID,task['id'],'resume',text)
        elif task['status'] in ACTIVE:
            store.control(OWNER_CHAT_ID,task['id'],'steer',text)
        else:
            raise ValueError('The delegate is not waiting on a question (status '+task['status']+').')
        task=store.get(task['id'])
    elif action in ('cancel','resume'):
        if action=='resume' and time.time()>row['deadline']:
            raise ValueError('Deadline expired. Inspect partial work and submit a new explicitly scoped request.')
        store.control(OWNER_CHAT_ID,task['id'],action)
        task=store.get(task['id'])
    elif action not in ('status','result'):
        raise ValueError('Use submit, status, result, answer, cancel or resume.')
    expired=time.time()>row['deadline'] and task['status'] in ('queued','starting','running','cancelling')
    if expired:
        store.control(OWNER_CHAT_ID,task['id'],'cancel')
        task=store.get(task['id'])
    states={'finished':'completed','needs_attention':'partial','interrupted':'blocked','starting':'queued','needs_answer':'question'}
    result=store.directory(task['id'])/'result.txt'
    health=ROOT/'task_state'/'transport.json'
    try:
        h=json.loads(health.read_text(encoding='utf-8'))
        available=time.time()-h.get('time',0)<120 and h.get('poll_ok',False)
    except (OSError,ValueError):
        available=False
    output={'request_id':request_id,'title':task['request'].splitlines()[0],
            'status':'timed_out' if expired else states.get(task['status'],task['status']),
            'task_id':task['id'],'worker_status':task['status'],'agent_available':available,'note':task['note'],
            'result_path':str(result) if result.exists() else None,
            'evidence_directory':str(store.directory(task['id'])/'task_state'/'actions'),
            'checkpoint_path':str(store.directory(task['id'])/'checkpoint.json'),
            'warning':'A saved answer is not verified research completion. No fallback collection is started.'}
    import task_cost
    output['budget']=task_cost.status(store.directory(task['id']))
    output['contract']=task_cost.contract(store.directory(task['id']))
    checkpoint=store.directory(task['id'])/'checkpoint.json'
    if checkpoint.exists():
        output['checkpoint']=checkpoint.read_text(encoding='utf-8')   # always: Cato sees progress while the job runs
    if task['status']=='needs_answer':
        output['question']=store.open_question(task['id'])
        output['note']='Waiting on your answer: delegate(action=\'answer\', request_id, note=...). Nothing is spent while it waits.'
    elif task['status']=='capped':
        output['note']='Paused at its budget; partial result saved. extend raises the cap and resumes; rework/rescope narrow the job.'
    elif task['status']=='infeasible':
        output['note']="The delegate's own estimate exceeds its cap; nothing more was spent. Rescope the completion items or extend the budget."
    if action=='result' and result.exists():
        output['text']=result.read_text(encoding='utf-8')
    return output

def expire_requests():
    """Local deadline enforcement, not scheduled source monitoring."""
    import threading
    def run():
        while True:
            try:
                store=TaskStore(ROOT)
                with store.db() as db:
                    exists=db.execute("SELECT 1 FROM sqlite_master WHERE name='research_requests'").fetchone()
                    rows=list(db.execute('SELECT request_id FROM research_requests WHERE deadline<?',(time.time(),))) if exists else []
                for row in rows:
                    call('status',request_id=row[0])
            except Exception as exc:
                print('[research-interface]',type(exc).__name__)
            time.sleep(10)
    threading.Thread(target=run,daemon=True,name='research-deadlines').start()

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['submit','status','result','cancel','resume','answer','extend','rework','rescope'])
    parser.add_argument('--request-file',type=Path)
    parser.add_argument('--request-id')
    args=parser.parse_args()
    try:
        request=json.loads(args.request_file.read_text(encoding='utf-8')) if args.request_file else None
        print(json.dumps(call(args.action,request,args.request_id),ensure_ascii=False,indent=2))
    except Exception as exc:
        print(json.dumps({'status':'failed','error':str(exc)},ensure_ascii=False))
        sys.exit(1)
