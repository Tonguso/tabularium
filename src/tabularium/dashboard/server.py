"""Loopback-only shared agent control panel. No model calls; independent of Telegram."""
from tabularium.paths import home as _home
from tabularium.ops import release
from pathlib import Path
import json, re, secrets, sqlite3, threading, time
from datetime import datetime
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
ROOT = _home()
STATIC = Path(__file__).resolve().parent
from tabularium.ops.operations import snapshot, read_json, query, PORT, URL
from tabularium.records.background_runtime import TaskStore
from tabularium.core.roster import AGENTS, ROLES
from tabularium.ops.scorecard import weekly
from tabularium.models.usage import aggregate, decode
from tabularium.core import trace
from tabularium.learning import regression
from tabularium.learning import research_signal
from tabularium.core.agent_runtime import atomic_json
from tabularium.models import provider_routing as routing
from tabularium.tools import search_routing
from tabularium.models.subscription_usage import snapshot as subscription_snapshot
from tabularium.channels.owner_telegram import OWNER_CHAT_ID
from tabularium.dashboard.history import history, export
from tabularium.channels import messaging
from tabularium.core.agent_runtime import DurableInbox
from tabularium.dashboard.audit import response as audit_response, format_audit
TOKEN = secrets.token_urlsafe(32)
# Phone access (2026-10-09): Tailscale Serve proxies the tailnet hostname to this loopback server and stamps every
# proxied request with Tailscale-User-Login. Only that hostname with Alex's login is accepted besides loopback.
TAILNET_HOST = 'your-pc.your-tailnet.ts.net'
TAILNET_LOGIN = 'owner@example.com'
ALLOWED_HOSTS = {f'127.0.0.1:{PORT}', TAILNET_HOST}
ALLOWED_ORIGINS = {URL, 'https://' + TAILNET_HOST}
CLAUDE_LOGIN_LOCK = threading.Lock()
TASK_STORES = {name: TaskStore(ROOT/name, ROLES[name]) for name in AGENTS}
TASKS = TASK_STORES['coordinator']

def task_store(name):
    if name not in TASK_STORES: raise ValueError('This agent has no background-task manager')
    return TASK_STORES[name]


def clean(value):
    text = str(value)
    text = re.sub(r'(?i)(?:sk-[A-Za-z0-9_-]{12,}|\b\d{8,12}:[A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9._-]+)', '[redacted]', text)
    text = re.sub(r'(?i)((?:api[_-]?key|access_token|refresh_token|password)[\s\"\x27:=]+)[^\s,\"\x27}]+', r'\1[redacted]', text)
    return text


def tail(name):
    path=ROOT/'logs'/f'{name}.log'
    if not path.exists(): return ''
    with path.open('rb') as stream:
        stream.seek(max(0,path.stat().st_size-18000))
        return clean(stream.read().decode('utf-8','replace'))


def usage(name):
    midnight=datetime.now(ZoneInfo('UTC')).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
    base=ROOT/name/'task_state'
    dbs=[base/'inbox.sqlite3']+list((base/'background').glob('*/task_state/inbox.sqlite3'))
    groups={}; recent=[]
    for db in dbs:
        try:
            recent += query(db,"SELECT provider,model,billing,role,created FROM usage_events WHERE kind='response' ORDER BY created DESC LIMIT 1")
            for event in query(db,"SELECT model,billing,role,metrics FROM usage_events WHERE kind='response' AND created>=?",(midnight,)):
                key=(event['model'],event['billing'],event['role'])
                row=groups.setdefault(key,dict(model=key[0],billing=key[1],role=key[2],calls=0,records=[]))
                row['calls']+=1; metrics=decode(event['metrics'], event['role'])
                row['records'].append(metrics)
        except (sqlite3.Error,ValueError): pass
    for row in groups.values():
        totals = aggregate(row.pop('records'))
        row.update(input=totals['input_total'], output=totals['output'], reported=totals,
                   unknown=max(totals['unknown_fields']['input_total'],totals['unknown_fields']['output']))
    return list(groups.values()), max(recent,key=lambda x:x['created']) if recent else None


def state():
    data=snapshot(ROOT/'coordinator',TASKS,OWNER_CHAT_ID)
    for name in AGENTS:
        folder=ROOT/name/'task_state'
        transport=read_json(folder/'transport.json')
        agent=data['agents'].setdefault(name,{})
        agent['name']=name; agent['transport']=transport
        if not (ROOT/name/'SKILL.md').exists():   # a slot this home has not set up: show how, not a stale heartbeat
            from tabularium.cli import next_steps
            agent.update(status='not set up',errors=[],setup={'command':f'tabularium --home {ROOT} init {name}','next':next_steps(ROOT,name)})
            continue
        ui=read_json(folder/'ui_state.json')
        agent['controls_live']=ui.get('pid')==agent['pid'] and time.time()-ui.get('updated',0)<15
        prefs=read_json(ROOT/name/f'model_{OWNER_CHAT_ID}.json')
        agent['model']=ui if agent['controls_live'] else {'provider':prefs.get('provider','unknown'),'model':prefs.get('anthropic_model',prefs.get('model','unknown')) if prefs.get('provider')!='openai' else prefs.get('openai_model','OpenAI (saved)'), 'options':[], 'route':'Saved preference; live controls not connected'}
        agent['routing'] = routing.read(ROOT/name/'routing.json')
        agent['routing_state'] = routing.read(folder/'routing_state.json')
        agent['routing_live'] = agent['controls_live'] and ui.get('routing_version') == 1
        if agent['routing'].get('enabled'):
            active=agent['routing_state'].get('route') if agent['routing_live'] else None
            preferred=agent['routing']['routes'][0]
            agent['model']=dict(agent['model'],model=(active or preferred)['model'],route=(('Last attempted: '+routing.label(active)) if active else 'Preferred: '+routing.label(preferred))+('' if agent['routing_live'] else ' · restart pending'))
        agent['usage'],agent['last_response']=usage(name)
        agent['errors']=[]
        if not agent['health_fresh']: agent['errors'].append('Agent heartbeat is stale. The process may have stopped.')
        elif not agent['polling_fresh']:agent['errors'].append('Telegram connection is not currently confirmed. Check the network and recent logs.')
        try:   # messages Telegram has not accepted yet (2026-10-22 21:47-21:57: nine minutes of 502s and timeouts, invisible)
            stuck=query(folder/'outbox.sqlite3',"SELECT count(*) n, min(created) since, max(attempts) tries, max(error) err FROM outbox WHERE status='pending'")
            if stuck and stuck[0]['n'] and time.time()-(stuck[0]['since'] or time.time())>60:
                agent['errors'].append(f"{stuck[0]['n']} message(s) waiting for Telegram since {time.strftime('%H:%M',time.localtime(stuck[0]['since']))} ({stuck[0]['tries']} attempts): {str(stuck[0]['err'] or '')[:120]}")
        except sqlite3.Error: pass
        try:
            # only the last seven days: a 12-day-old failure sat on the card as "recent" until 2026-10-24
            failures=query(folder/'inbox.sqlite3',"SELECT id,created,status FROM jobs WHERE chat=? AND status IN ('failed','interrupted') AND created>? ORDER BY id DESC LIMIT 4",(OWNER_CHAT_ID,time.time()-7*86400))
            agent['failures']=failures
        except sqlite3.Error:agent['failures']=[]
        agent['commands']=[read_json(p) for p in sorted((folder/'ui_commands').glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:3]]
        # Routes currently skipped and why (route_<kind>.json cooldowns): a fallback must be visible, not silent.
        agent['route_blocks']=[]
        for kind in ('anthropic_subscription','openai_subscription','anthropic_api','openai_api'):
            block=read_json(folder/f'route_{kind}.json')
            if block.get('retry_at',0)>time.time():
                agent['route_blocks'].append({'kind':kind,'reason':block.get('reason',''),'retry_at':block['retry_at']})
                agent['errors'].append(routing.KINDS.get(kind,kind)+' is being skipped: '+str(block.get('reason',''))[:160])
        # Liveness that means something (2026-10-11): did a real turn complete, did any fail, did the self-test pass.
        try:
            ok=query(folder/'inbox.sqlite3',"SELECT max(created) AS t FROM jobs WHERE chat=? AND status='done'",(OWNER_CHAT_ID,))
            agent['last_ok_reply']=(ok[0]['t'] if ok and isinstance(ok[0],dict) else (ok[0][0] if ok else None))
            failed=query(folder/'inbox.sqlite3',"SELECT count(*) AS n, max(created) AS t FROM jobs WHERE chat=? AND status='failed' AND created>?",(OWNER_CHAT_ID,time.time()-86400))
            row=failed[0] if failed else {}
            agent['failed_24h']=(row['n'] if isinstance(row,dict) else row[0]) if row else 0
            last_fail=(row['t'] if isinstance(row,dict) else row[1]) if row else None
            if agent['failed_24h'] and (not agent['last_ok_reply'] or (last_fail or 0)>agent['last_ok_reply']):
                agent['errors'].append(f"{agent['failed_24h']} turn(s) failed in the last 24 h and none has succeeded since. Read the log.")
        except (sqlite3.Error,TypeError,KeyError,IndexError):
            agent['last_ok_reply']=None; agent['failed_24h']=0
        agent['selftest']=read_json(folder/'selftest.json')
        try:   # skill edits awaiting Alex's go (2026-11-01: 40 proposals had sat unseen in the register)
            from tabularium.learning import learning
            agent['lessons']=learning.proposals(ROOT/name,OWNER_CHAT_ID)
        except Exception as exc:
            agent['lessons']=[]; agent['errors'].append('Lesson register unreadable: '+str(exc)[:120])
        try:   # method reviews (2026-11-10): dockets the runtime proposed or the agent confirmed, with the review's state
            from tabularium.learning import dockets
            agent['reviews']=dockets.reviews(ROOT/name,OWNER_CHAT_ID,TASK_STORES.get(name)) if ROLES[name].learns else []
        except Exception as exc:
            agent['reviews']=[]; agent['errors'].append('Docket register unreadable: '+str(exc)[:120])
        try:   # questions waiting on Alex (2026-11-02: 19 of Cato's stood in the store, shown only inside Messaging).
               # Parked actions and background tasks' questions only: a foreground question is the conversation he is
               # already in, not a loose end (2026-11-08: the logo back-and-forth filled the list).
            agent['questions']=[{'id':d['id'],'task':d['task'],'kind':d.get('kind','question'),'question':d['question'],'created':d['created']}
                                for d in task_store(name).decisions(OWNER_CHAT_ID) if d.get('kind')=='action' or d['task']]
        except Exception as exc:
            agent['questions']=[]; agent['errors'].append('Decision store unreadable: '+str(exc)[:120])
        # Foreground usage today (local day): what one message costs on average, so compaction is measurable.
        try:
            midnight=datetime.now(ZoneInfo('UTC')).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
            rows=query(folder/'inbox.sqlite3',"SELECT role,metrics FROM usage_events WHERE kind='response' AND created>?",(midnight,))
            turns=sum(r.get('role') not in ('reviewer','advisor') for r in rows)
            totals=aggregate(decode(r.get('metrics'), r.get('role')) for r in rows)
            inp,cached=totals['input_total'],totals['cache_read']
            agent['today']={'turns':turns,'input_per_turn':int(inp/turns) if turns and inp is not None else None,
                            'output':totals['output'],'cache_pct':int(100*cached/inp) if inp and cached is not None else None}
        except (sqlite3.Error,ValueError,TypeError):
            agent['today']={'turns':0,'input_per_turn':None,'output':None,'cache_pct':None}
        if agent['selftest'] and not agent['selftest'].get('ok'):
            agent['errors'].append('Daily self-test failed: a real turn does not complete. '+str(agent['selftest'].get('error') or '')[:160])
        elif agent['selftest'] and agent['selftest'].get('fallback'):
            agent['errors'].append('Self-test passed on the fallback route ('+str(agent['selftest'].get('route'))+'), not the first choice.')
        # Weekly memory checks (2026-10-11): the exam says whether he remembers this week's facts, the review how much
        # of memory.md is ready for a clean-up he must propose to Alex first.
        agent['memory_exam']=read_json(folder/'memory_exam.json')
        agent['memory_review']=read_json(folder/'memory_review.json')
        try:   # the latest regression eval against the one before (2026-10-23)
            agent['regression']=regression.latest(ROOT/name)
            r=agent['regression']
            if r and r.get('previous_pass_1') is not None and r.get('n') and r.get('previous_n') and \
               r['pass_1']/r['n'] < r['previous_pass_1']/r['previous_n'] - 0.2:
                agent['errors'].append(f"Regression eval fell: pass@1 {r['pass_1']}/{r['n']} against {r['previous_pass_1']}/{r['previous_n']} last time. Something changed for the worse; read task_state/regression/.")
        except (OSError, ValueError, ZeroDivisionError): agent['regression']=None
        try:   # research-quality signal on real cases: closer / same / further, never a score (2026-10-24)
            agent['research_signal']=research_signal.latest(ROOT/name)
            s=agent['research_signal']
            if s and s.get('drifting'):
                agent['errors'].append(f"Research signal: the last {s['runs']} weekly reruns of real cases came out further from the accepted answers than closer. Something changed for the worse; read task_state/research_signal/.")
        except (OSError, ValueError): agent['research_signal']=None
        if agent['memory_exam'] and not agent['memory_exam'].get('ok'):
            wrong=[i for i in agent['memory_exam'].get('items',[]) if i.get('score')=='wrong']
            detail='; '.join(f"Q: {str(i.get('question',''))[:90]} / expected: {str(i.get('expected',''))[:60]}" for i in wrong[:3])
            agent['errors'].append(f"Weekly memory exam failed: {len(wrong)} wrong answer(s). {detail}")
    data['tasks']=[]
    for name,store in TASK_STORES.items():
        for task in store.status(OWNER_CHAT_ID):
            task['agent']=name
            title=(task.get('request') or 'Background work').splitlines()[0]
            task['label']=(title[:100]+'…') if len(title)>100 else title
            task['checkpoint']=read_json(store.directory(task['id'])/'checkpoint.json')
            task['completion']=read_json(store.directory(task['id'])/'completion.json')   # checks live here, not in his messages (2026-10-24)
            data['tasks'].append(task)
    data['restart']=read_json(ROOT/'dashboard/restart_status.json')
    data['supervisor']=read_json(ROOT/'logs/supervisor_state.json')
    data['subscriptions']=subscription_snapshot()
    data['csrf']=TOKEN
    data['search_order']=search_routing.read_order()
    data['claude_login']=read_json(ROOT/'dashboard/claude_login.json')
    data['quota']=read_json(ROOT/'coordinator/task_state/search_quota.json')
    data['usage_note']='Recorded responses today (local time). Missing measurements are flagged. Subscription allowance, voice charges and dollar costs are not inferred.'
    data['recovery']='Model changes apply between foreground turns. Background workers keep their current model. Resume only after reviewing prior actions to avoid repeating side effects.'
    return data


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def allowed(self,write=False,media=False):
        host=self.headers.get('Host')
        if host not in ALLOWED_HOSTS or self.headers.get('Sec-Fetch-Site')=='cross-site':return False
        if host==TAILNET_HOST and self.headers.get('Tailscale-User-Login')!=TAILNET_LOGIN:return False
        origin=self.headers.get('Origin')
        if origin and origin not in ALLOWED_ORIGINS:return False
        return not write or (origin in ALLOWED_ORIGINS and self.headers.get('X-CSRF-Token')==TOKEN and self.headers.get('Content-Type','').split(';')[0]==('application/octet-stream' if media else 'application/json'))
    def send(self,body,kind='application/json',code=200,filename=None):
        if not isinstance(body,bytes):body=json.dumps(body,ensure_ascii=False).encode('utf-8')
        self.send_response(code);self.send_header('Content-Type',kind);self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff');self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'")
        if filename:self.send_header('Content-Disposition', 'attachment; filename="'+filename+'"')
        self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    def do_GET(self):
        if not self.allowed():self.send_error(403);return
        url=urlsplit(self.path);args=parse_qs(url.query)
        try:
            static={'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','text/javascript; charset=utf-8'),'/style.css':('style.css','text/css; charset=utf-8')}
            static['/messaging.js']=('messaging.js','text/javascript; charset=utf-8')
            static['/agents.apk']=('android/build/agents.apk','application/vnd.android.package-archive')
            for name in AGENTS:
                static['/assets/'+name+'.jpg']=('assets/'+name+'.svg','image/svg+xml')
            if not (STATIC/'static/assets/librarian.jpg').exists():
                static['/assets/librarian.jpg']=('assets/librarian.svg','image/svg+xml')
            if not (STATIC/'static/assets/engineer.jpg').exists():
                static['/assets/engineer.jpg']=('assets/engineer.svg','image/svg+xml')
            static['/assets/tabularium.svg']=('assets/tabularium.svg','image/svg+xml')
            static['/assets/tabularium.svg']=('assets/tabularium.svg','image/svg+xml')
            static['/assets/tabularium.svg']=('assets/tabularium.svg','image/svg+xml')
            static['/manifest.webmanifest']=('manifest.webmanifest','application/manifest+json')
            static['/assets/app-icon-512.png']=('assets/tabularium.svg','image/svg+xml')
            static['/assets/apple-touch.png']=('assets/tabularium.svg','image/svg+xml')
            if url.path in static:
                filename,kind=static[url.path]
                self.send((STATIC/('static/'+filename if not filename.startswith('android/') else filename)).read_bytes(),kind)
            elif url.path=='/api/messages':
                name=args.get('agent',[''])[0]
                if name not in AGENTS:raise ValueError('Unknown agent')
                data=messaging.conversation(ROOT/name, OWNER_CHAT_ID)
                data['decisions']=[{'id':d['id'],'question':d['question'],'kind':d.get('kind','question')} for d in task_store(name).decisions(OWNER_CHAT_ID)]
                data['ready']=messaging.ready(ROOT/name)
                data['media_ready']=messaging.ready(ROOT/name,media=True)
                self.send(data)
            elif url.path=='/api/state':self.send(state())
            elif url.path=='/api/scorecard':self.send(weekly(ROOT))
            elif url.path in ('/api/history','/api/history/export'):
                data=history(args.get('agent',['coordinator'])[0],OWNER_CHAT_ID,args.get('from',[''])[0],args.get('to',[''])[0],args.get('q',[''])[0])
                if url.path.endswith('/export'):
                    fmt=args.get('format',['md'])[0]
                    body,kind=export(data,fmt)
                    filename=data['agent']+'-history-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.'+fmt
                    self.send(body,kind,filename=filename)
                else:
                    offset=max(0,int(args.get('offset',['0'])[0]));limit=30
                    latest=list(reversed(data['messages']))
                    data.update(messages=[{**r,'text':r['text'][:6000],'truncated':len(r['text'])>6000} for r in latest[offset:offset+limit]],offset=offset,has_more=offset+limit<len(latest))
                    self.send(data)
            elif url.path in ('/api/communications','/api/communications/detail','/api/communications/export'):
                mode='export' if url.path.endswith('/export') else 'detail' if url.path.endswith('/detail') else 'list'
                data=audit_response(args,mode)
                if mode=='export':
                    fmt=args.get('format',['json'])[0]
                    body=format_audit(data,fmt).encode('utf-8')
                    kind={'json':'application/json','jsonl':'application/x-ndjson','txt':'text/plain; charset=utf-8'}[fmt]
                    self.send(body,kind,filename='inter-agent-audit.'+fmt)
                else:self.send(data)
            elif url.path=='/api/log':
                name=args.get('agent',[''])[0]
                if name not in AGENTS:raise ValueError('Unknown agent')
                self.send({'text':tail(name)})
            elif url.path=='/api/trace':
                store=task_store(args.get('agent',['coordinator'])[0])
                task=store.get(int(args.get('task',['0'])[0]))
                if task['chat']!=OWNER_CHAT_ID:raise ValueError('Wrong owner')
                evs=trace.events(store,task['id'])
                self.send({'task':task['id'],'label':store.label(task),'status':task['status'],'summary':trace.summary(evs),'events':evs})
            elif url.path=='/api/result':
                store=task_store(args.get('agent',['coordinator'])[0])
                task=store.get(int(args.get('task',['0'])[0]))
                if task['chat']!=OWNER_CHAT_ID:raise ValueError('Wrong owner')
                path=store.directory(task['id'])/'result.txt'
                self.send({'text':clean(path.read_text(encoding='utf-8')[:120000]) if path.exists() else 'No saved result yet.'})
            else:self.send_error(404)
        except (ValueError,FileNotFoundError):self.send({'error':'Requested item is unavailable.'},code=400)
        except Exception as exc:
            print('[dashboard]',type(exc).__name__);self.send({'error':'Status is temporarily unavailable; try again.'},code=503)
    def do_POST(self):
        upload = urlsplit(self.path).path == '/api/attachments'
        if not self.allowed(True,media=upload):self.send_error(403);return
        try:
            size=int(self.headers.get('Content-Length','0'))
            if upload:
                args=parse_qs(urlsplit(self.path).query)
                name=args.get('agent',[''])[0]
                if name not in AGENTS:raise ValueError('Unknown agent')
                if not messaging.ready(ROOT/name,media=True):raise ValueError('This agent is not ready for attachments yet')
                self.connection.settimeout(60)
                self.send(messaging.store_attachment(ROOT/name,args.get('name',[''])[0],self.rfile,size))
                return
            if not 0<size<=(32768 if self.path=='/api/messages' else 4096):raise ValueError('Invalid request size')
            value=json.loads(self.rfile.read(size))
            if self.path=='/api/messages':
                name=value.get('agent')
                if name not in AGENTS:raise ValueError('Unknown agent')
                if not messaging.ready(ROOT/name,media=value.get('attachment') is not None):raise ValueError('This agent is offline or has not loaded Messaging. Your message was not queued.')
                inbox=DurableInbox(ROOT/name, lambda *args: None)
                self.send(inbox.accept_dashboard(value.get('client_id'),value.get('text'),value.get('decision_id',0),attachment=value.get('attachment')))
                return
            if self.path=='/api/subscriptions/refresh':
                subscription_snapshot(force=True)
                self.send({'message':'Allowance refresh requested; reads are limited to once every 30 seconds.'}); return
            if self.path in ('/api/claude-connect','/api/connect-subscriptions'):
                if not CLAUDE_LOGIN_LOCK.acquire(blocking=False):
                    raise ValueError('Subscription sign-in is already in progress.')
                from tabularium.models.subscription_connections import connect_all, connect_claude, report
                all_agents=self.path=='/api/connect-subscriptions'
                report('starting','Checking subscription connections…')
                def login():
                    try:
                        if all_agents:
                            connect_all()
                        elif connect_claude():
                            for agent in AGENTS:
                                atomic_json(ROOT/agent/'task_state/route_anthropic_subscription.json',{})
                            report('connected','Claude subscription connected for all four agents.')
                        else:
                            report('failed','Claude sign-in did not complete.')
                    except Exception as exc:
                        # OAuth exception bodies may contain credentials. Keep dashboard errors generic.
                        report('failed','Subscription sign-in did not complete ('+type(exc).__name__+'). Existing connections are preserved.')
                    finally:
                        CLAUDE_LOGIN_LOCK.release()
                threading.Thread(target=login,daemon=True).start()
                self.send({'message':'Connecting subscriptions. Complete any authorization pages opened in your browser.'}); return
            if self.path=='/api/routing':
                name=value.get('agent')
                if name not in AGENTS: raise ValueError('Unknown agent')
                routing.save(ROOT/name,value.get('routing'))
                self.send({'message':'Route order saved. Updated runtimes read it at the next turn; older runtimes require restart.'}); return
            if self.path=='/api/search_order':
                search_routing.save_order(value.get('order'))
                self.send({'message':'Search order saved for all four agents; it applies to their next search, no restart.'}); return
            if self.path=='/api/restart':
                selected=value.get('agent','all')
                if selected not in ('all',)+AGENTS:raise ValueError('Unknown agent')
                names=AGENTS if selected=='all' else (selected,)
                # A restart is a flag (2026-10-09): the agent finishes queued work, exits, and the supervisor restarts it.
                from tabularium.ops.agent_control import request_restart
                request_restart(names)
                self.send({'message':'Release queued. The supervisor will test and update the agents and dashboard.'}); return
            elif self.path=='/api/task':
                action=value.get('action')
                if action not in ('cancel','resume'):raise ValueError('Unsupported task action')
                name=value.get('agent','coordinator')
                store=task_store(name)
                health=read_json(ROOT/name/'task_state/health.json')
                if time.time()-health.get('last_progress',0)>150:raise ValueError(name.title()+' is offline; start the agent before changing tasks.')
                task=store.get(int(value['task']))
                if action=='resume' and task['status'] not in ('failed','interrupted','cancelled'):
                    raise ValueError('Only stopped, failed or cancelled tasks can be resumed.')
                store.control(OWNER_CHAT_ID,task['id'],action)
                self.send({'message':'Cancellation requested; the worker stops at a safe boundary.' if action=='cancel' else 'Resume queued; the worker has not necessarily started yet.'})
            else:self.send_error(404)
        except (ValueError,KeyError,TypeError) as exc:self.send({'error':clean(exc)},code=400)
        except Exception as exc:
            print('[dashboard action]',type(exc).__name__);self.send({'error':'Action could not be completed. Refresh status before retrying.'},code=503)


class _ExclusiveServer(ThreadingHTTPServer):
    """HTTPServer enables SO_REUSEADDR, and on Windows that lets a second dashboard bind the
    same port instead of failing -- two were serving 8220 at once on 2026-10-06. Exclusive use
    makes the second bind fail, so the duplicate exits."""
    allow_reuse_address = False

    def server_bind(self):
        import socket
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def serve(server, root=ROOT):
    """Stop accepting requests on drain, finish handlers, then acknowledge the release."""
    import os
    from tabularium.ops.supervisor import read_json, RESTART_EXIT
    health = {'pid': os.getpid(), 'instance': os.environ.get('AGENT_LAUNCH_ID'), 'release': release.LOADED}
    server.timeout = 1
    try:
        while True:
            health.update(phase='idle', last_progress=time.time())
            atomic_json(root/'dashboard/task_state/health.json', health)
            request = read_json(root/'dashboard/task_state/drain')
            if request.get('id') and request.get('instance') == health['instance']:
                break
            server.handle_request()
    finally:
        server.server_close()  # ThreadingHTTPServer waits for in-flight handlers.
    health.update(phase='restart_ready', restart=request, last_progress=time.time())
    atomic_json(root/'dashboard/task_state/health.json', health)
    return RESTART_EXIT


def main():
    import os
    from tabularium.core.agent_logging import start_logging
    if not release.same_source(release.identity(), release.LOADED):
        raise RuntimeError('Source changed during dashboard startup')
    try:server=_ExclusiveServer(('127.0.0.1',PORT),Handler)
    except OSError:
        print('Dashboard already running or port occupied.');return
    start_logging('dashboard')
    print('Agent dashboard:',URL)
    os._exit(serve(server))


if __name__=='__main__':main()
