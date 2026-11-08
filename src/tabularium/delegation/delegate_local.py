"""Audited local Vit/Cal CLI. No agent launch, provider change or Telegram send.
python delegate_local.py vit|cal submit --request-file request.json
python delegate_local.py vit|cal status|result|cancel|resume --request-id ID
python delegate_local.py vit|cal extend --request-file extend.json   ({request_id, budget_effective_tokens})
python delegate_local.py export --agent vit --request-id ID --date YYYY-MM-DD --out PATH_PREFIX
Export reads remote evidence without calling clients (status can enforce deadlines).
"""
from tabularium.paths import child_env
from tabularium.paths import home as _home
import argparse
from contextlib import closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import uuid

ROOT = _home()/'cato'
AGENTS = {'vit': _home()/'vitruvius', 'cal': _home()/'callimachus'}
CLIENTS = {'vit': 'tabularium.delegation.work_client', 'cal': 'tabularium.delegation.research_client'}
DB = ROOT/'reports'/'delegation_audit'/'events.sqlite3'
SECRET = re.compile(r'(?:sk-[A-Za-z0-9_-]{20,}|\b\d{7,12}:[A-Za-z0-9_-]{25,}|Bearer\s+\S+|-----BEGIN .*PRIVATE KEY|(?:api[_ -]?key|password|access_token|refresh_token)\s*[=:]\s*[^\s,;]+)', re.I)
PRIVATE = {'thinking','reasoning','chain_of_thought','analysis','credentials','authorization','api_key','password','access_token','refresh_token'}

def safe(value):
    if isinstance(value, dict):
        return {k: '[EXCLUDED]' if k.lower() in PRIVATE else safe(v) for k,v in value.items()}
    if isinstance(value, list):
        return [safe(v) for v in value]
    return SECRET.sub('[CREDENTIAL EXCLUDED]',value) if isinstance(value,str) else value

def clean_input(text):
    value=json.loads(text)
    if safe(value)!=value or SECRET.search(text):
        raise ValueError('Credential/private-reasoning fields detected. Remove before submitting; nothing submitted.')
    return value

def connection(path=None):
    path=DB if path is None else path
    path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=30)
    db.execute('CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, timestamp TEXT, agent TEXT, request_id TEXT, payload TEXT)')
    return db

def record(db, agent, request_id, payload):
    now=datetime.now().astimezone().isoformat()
    ident=str(uuid.uuid4())
    returned=payload.get('phase')=='return'
    event={'event_id':ident,'timestamp':now,'agent':agent,'sender':agent if returned else 'cato','recipient':'cato' if returned else agent,'request_id':request_id,**safe(payload)}
    with db:
        db.execute('INSERT INTO events VALUES(?,?,?,?,?)',(ident,now,agent,request_id,json.dumps(event,ensure_ascii=False)))
    return ident

def invoke(agent, action, request_file=None, request_id=None, message=''):
    raw=request_file.read_text(encoding='utf-8-sig') if request_file else None
    request=clean_input(raw) if raw else None
    if SECRET.search(message):
        raise ValueError('Remove credentials from message.')
    request_id=request['request_id'] if request else request_id
    if not request_id:
        raise ValueError('request_id required')
    db=connection()
    operation=record(db,agent,request_id,{'phase':'intent','action':action,'request_raw':raw,'request':request,'message':message,'outcome':'not_yet_observed'})
    args=[sys.executable,'-m',CLIENTS[agent],action]
    temp=None
    try:
        if request:
            scratch=ROOT/'scratch'; scratch.mkdir(exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',suffix='.json',dir=scratch,delete=False) as file:
                file.write(raw); temp=Path(file.name)
            args+=['--request-file',str(temp)]
        else:
            args+=['--request-id',request_id]
        if message:
            if agent!='vit': raise ValueError('Cal does not support steering messages')
            args+=['--message',message]
        result=subprocess.run(args,capture_output=True,encoding='utf-8',errors='strict',timeout=50,env=child_env(PYTHONIOENCODING='utf-8'))
        try: response=json.loads(result.stdout)
        except ValueError: response={'unparsed_stdout':result.stdout}
        record(db,agent,request_id,{'phase':'return','action':action,'operation_id':operation,'returncode':result.returncode,'response':response,'stderr':result.stderr,'outcome':'client_returned'})
        return response,result.returncode
    except Exception as exc:
        record(db,agent,request_id,{'phase':'failure','action':action,'operation_id':operation,'error':str(exc),'outcome':'unknown_remote_outcome_inspect_before_retry'})
        raise
    finally:
        if temp: temp.unlink(missing_ok=True)
        db.close()

def read_json(path):
    return safe(json.loads(path.read_text(encoding='utf-8')))

def audit_events(agent=None,request_id=None,date=None,max_events=None):
    if agent and agent not in AGENTS:
        raise ValueError('Unsupported audit agent')
    if not DB.exists():
        return []
    with closing(sqlite3.connect(DB.as_uri()+'?mode=ro',uri=True)) as db:
        sql='SELECT payload FROM events WHERE (? IS NULL OR agent=?) AND (? IS NULL OR request_id=?) AND (? IS NULL OR substr(timestamp,1,10)=?) ORDER BY timestamp,id'
        rows=db.execute(sql,(agent,agent,request_id,request_id,date,date))
        events=[]; size=0
        for row in rows:
            size+=len(row[0])
            if max_events is not None and (len(events)>=max_events or size>4000000):
                raise ValueError('Audit selection exceeds safe limit; narrow agent/date/request ID filters')
            events.append(json.loads(row[0]))
    for event in events:
        if 'agent' not in event:
            event['agent']=event['recipient']
            if event.get('phase')=='return':
                event['sender'],event['recipient']=event['agent'],'cato'
                event['export_normalization']='Direction corrected from initial wrapper schema; original stored event retained.'
    events=[e for e in events if (not agent or e.get('agent',e['recipient'])==agent) and (not request_id or e['request_id']==request_id) and (not date or e['timestamp'][:10]==date)]
    return safe([e for e in events if e.get('agent') in AGENTS])

def audit_data(agent=None,request_id=None,date=None,events=None,max_bytes=None,owner_chat=None):
    events=audit_events(agent,request_id,date) if events is None else safe(events)
    snapshots=[]
    for target,rid in sorted({(e['agent'],e['request_id']) for e in events if e.get('agent') in AGENTS}):
        base=AGENTS[target]; database=base/'task_state'/'background'/'jobs.sqlite3'
        table='engineering_requests' if target=='vit' else 'research_requests'
        snap={'agent':target,'request_id':rid,'observed_at':datetime.now().astimezone().isoformat(),'evidence':[]}
        try:
            db=sqlite3.connect(database.as_uri()+'?mode=ro',uri=True); db.row_factory=sqlite3.Row
            mapping=db.execute('SELECT * FROM '+table+' WHERE request_id=?',(rid,)).fetchone()
            if mapping:
                task=dict(db.execute('SELECT * FROM tasks WHERE id=?',(mapping['task'],)).fetchone())
                if owner_chat is not None and task.get('chat')!=owner_chat:
                    snap['limitation']='Remote task belongs to another owner; evidence excluded.'
                    snapshots.append(snap);db.close();continue
                snap.update(mapping=safe(dict(mapping)),task=safe(task),controls=safe([dict(r) for r in db.execute('SELECT * FROM controls WHERE task=?',(task['id'],))]))
                directory=database.parent/str(task['id'])
                usage=directory/'task_state'/'inbox.sqlite3'
                if usage.exists():
                    with closing(sqlite3.connect(usage.as_uri()+'?mode=ro',uri=True)) as usage_db:
                        usage_db.row_factory=sqlite3.Row
                        snap['usage_events']=safe([dict(row) for row in usage_db.execute('SELECT * FROM usage_events ORDER BY id')])
                paths=[directory/name for name in ('context.json','checkpoint.json','result.txt')]
                paths+=sorted((directory/'task_state'/'actions').glob('*.json'))
                for path in paths:
                    if path.exists():
                        if max_bytes is not None:
                            if path.stat().st_size>max_bytes:
                                snap['evidence'].append({'path':str(path),'limitation':'Evidence too large for dashboard; use local CLI export.'})
                                continue
                            max_bytes-=path.stat().st_size
                        raw=path.read_text(encoding='utf-8')
                        content=read_json(path) if path.suffix=='.json' else safe(raw)
                        snap['evidence'].append({'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'content':content})
            else: snap['limitation']='No mapped remote task found.'
            db.close()
        except (OSError,sqlite3.Error,ValueError) as exc: snap['limitation']=str(exc)
        snapshots.append(snap)
    output={'exported_at':datetime.now().astimezone().isoformat(),'limitations':['Mutable local audit, not tamper-proof.','Exact credential-free submitted input; pattern filtering is not comprehensive DLP.','Observed lifecycle and saved tool records, not inaccessible private reasoning.','Other routes bypassing this wrapper are not automatically captured.','Date selects local communication events; snapshots show current state, not complete historical state transitions.'], 'events':events,'snapshots':snapshots}
    return output

def format_audit(output,fmt):
    if fmt=='json':
        return json.dumps(output,ensure_ascii=False,indent=2)
    if fmt=='jsonl':
        return ''.join(json.dumps({'kind':'communication',**e},ensure_ascii=False)+'\n' for e in output['events'])+''.join(json.dumps({'kind':'remote_snapshot',**s},ensure_ascii=False)+'\n' for s in output['snapshots'])
    if fmt=='txt':
        return 'LOCAL DELEGATION AUDIT\n'+'\n'.join(output['limitations'])+'\n\n'+json.dumps({'events':output['events'],'snapshots':output['snapshots']},ensure_ascii=False,indent=2)
    raise ValueError('Unsupported export format')

def export(prefix,agent=None,request_id=None,date=None):
    output=audit_data(agent,request_id,date)
    prefix.parent.mkdir(parents=True,exist_ok=True)
    files=[Path(str(prefix)+'.'+fmt) for fmt in ('json','jsonl','txt')]
    for path in files:
        path.write_text(format_audit(output,path.suffix[1:]),encoding='utf-8')
    return {'files':[str(p) for p in files],'events':len(output['events']),'snapshots':len(output['snapshots'])}

def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('agent',choices=['vit','cal','export'])
    parser.add_argument('action',nargs='?',choices=['submit','status','result','cancel','resume','steer','answer','extend','rework','rescope'])
    parser.add_argument('--request-file',type=Path); parser.add_argument('--request-id'); parser.add_argument('--message',default='')
    parser.add_argument('--agent',dest='filter_agent',choices=['vit','cal']); parser.add_argument('--date'); parser.add_argument('--out',type=Path)
    args=parser.parse_args()
    if args.agent=='export':
        if not args.out: parser.error('--out required')
        result=export(args.out,args.filter_agent,args.request_id,args.date); code=0
    else:
        if not args.action: parser.error('action required')
        result,code=invoke(args.agent,args.action,args.request_file,args.request_id,args.message)
    print(json.dumps(result,ensure_ascii=False,indent=2)); return code

if __name__=='__main__':
    raise SystemExit(main())
