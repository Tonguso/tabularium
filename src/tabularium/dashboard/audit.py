"""Read-only view of Cato's audited wrapper. No arbitrary path parameters."""
import json,re
from pathlib import Path
from tabularium.paths import home as _home
from tabularium.delegation.delegate_local import audit_events,audit_data,format_audit
from tabularium.channels.owner_telegram import OWNER_CHAT_ID
COVERAGE='Cato audited wrapper only, not all agent traffic. Vit and Cal only; Tacitus excluded. No hidden reasoning. Mutable local logs are not tamper-proof. Credential pattern filtering is not comprehensive DLP. Date filters select communication events; status is last observed within the selection, not live state. Remote snapshots are current saved evidence.'

def selection(args):
    get=lambda key:args.get(key,[''])[0]
    agent,date,rid,status=get('agent'),get('date'),get('request_id'),get('status')
    if agent and agent not in ('vit','cal'):raise ValueError('Unsupported agent')
    if date and not re.fullmatch(r'\d{4}-\d{2}-\d{2}',date):raise ValueError('Invalid date')
    if len(rid)>200 or any(ord(c)<32 for c in rid):raise ValueError('Invalid request ID')
    if status and not re.fullmatch(r'[a-z_ -]{1,50}',status):raise ValueError('Invalid status')
    events=audit_events(agent or None,rid or None,date or None,max_events=5000)
    groups={}
    for event in events:
        key=(event['agent'],event['request_id'])
        row=groups.setdefault(key,dict(agent=key[0],request_id=key[1],first=event['timestamp'],last=event['timestamp'],status='unobserved',events=0,title=''))
        row['last']=event['timestamp'];row['events']+=1
        request=event.get('request') or {}
        row['title']=request.get('title') or row['title']
        response=event.get('response') or {}
        if isinstance(response,dict):row['status']=response.get('status') or row['status']
        if event.get('phase')=='failure' or event.get('returncode',0)!=0:row['status']='failure'
    statuses=sorted({r['status'] for r in groups.values()})
    rows=[r for r in groups.values() if not status or r['status']==status]
    keys={(r['agent'],r['request_id']) for r in rows}
    return sorted(rows,key=lambda r:r['last'],reverse=True),[e for e in events if (e['agent'],e['request_id']) in keys],statuses

def acceptance(request_id):
    """Cato's side of one request: purpose, source, status, per-item verdicts and the brief lesson (jobs.sqlite3)."""
    import sqlite3
    from contextlib import closing
    path=_home()/'cato'/'task_state'/'background'/'jobs.sqlite3'
    if not request_id or not path.exists():return None
    try:
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=5)) as con:
            con.row_factory=sqlite3.Row
            d=con.execute('SELECT task,agent,purpose,source,status,acceptance,created,updated FROM delegations WHERE request_id=? ORDER BY created DESC LIMIT 1',(request_id,)).fetchone()
            card=con.execute('SELECT task_type,budget,spent,items_total,items_met,critical_total,critical_met,reworks,brief_lesson,reason FROM scorecard WHERE request_id=?',(request_id,)).fetchone()
    except sqlite3.Error:
        return None
    if not d and not card:return None
    out=dict(d) if d else {}
    try:out['verdicts']=json.loads(out.get('acceptance') or '[]')
    except ValueError:out['verdicts']=[]
    out.pop('acceptance',None)
    if card:out['scorecard']=dict(card)
    return out

def response(args,mode):
    rows,events,statuses=selection(args)
    if mode=='list':
        offset=int(args.get('offset',['0'])[0])
        if not 0<=offset<=100000:raise ValueError('Invalid offset')
        return dict(coverage=COVERAGE,requests=rows[offset:offset+30],count=len(rows),offset=offset,has_more=offset+30<len(rows),statuses=statuses)
    if mode=='detail' and (len(rows)!=1 or not args.get('request_id') or not args.get('agent')):
        raise ValueError('Choose one agent and request ID')
    if len(rows)>100:raise ValueError('Export limited to 100 requests; narrow filters')
    data=audit_data(events=events,max_bytes=4000000,owner_chat=OWNER_CHAT_ID)
    data['limitations'].insert(0,COVERAGE)
    if mode=='detail':
        data['acceptance']=acceptance(args.get('request_id',[''])[0])
    if len(json.dumps(data,ensure_ascii=False).encode('utf-8'))>8000000:
        raise ValueError('Selection exceeds 8 MB; narrow filters or use local CLI export')
    return data
