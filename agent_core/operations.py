"""Shared operations snapshot and bounded capability probes for the independent dashboard."""
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from agent_runtime import atomic_json
from agent_core.memory_store import memory_lock
PORT=8220
URL=f"http://127.0.0.1:{PORT}"

def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError,ValueError):
        return {}

def query(path,sql,args=()):
    if not Path(path).exists():
        return []
    with closing(sqlite3.connect("file:"+Path(path).as_posix()+"?mode=ro",uri=True,timeout=3)) as db:
        db.row_factory=sqlite3.Row
        return [dict(r) for r in db.execute(sql,args)]

def capabilities(root, probe="overview"):
    root=Path(root)
    if probe not in ("overview","outlook"):
        raise ValueError("Use overview or outlook.")
    now=time.time()
    result={"checked_at":now,"telegram":read_json(root/"task_state"/"transport.json"),
            "browser":{"status":"configured" if (root/"browser_profile").exists() else "missing",
                       "note":"Profile existence does not prove login or membership. Use headless browser_navigate for the requested site."},
            "pdf":{"status":"installed" if importlib.util.find_spec("pypdf") else "needs_check",
                   "note":"Successful extraction must be checked on the actual PDF; scanned pages may need OCR."},
            "voice":{"status":"configured" if os.environ.get("ELEVENLABS_API_KEY") else "missing_key",
                     "provider":"ElevenLabs", "model":"eleven_v4",
                     "voice_id":os.environ.get(root.name.upper()+"_ELEVENLABS_VOICE_ID", "c6SfcYrb2t09NHXiT80T"),
                     "note":"No paid audio request made; this does not prove service availability."},
            "outlook":{"status":"not_probed","note":"Run outlook probe; never launch setup or send test mail."}}
    dbs=[root/"task_state"/"inbox.sqlite3"]+list((root/"task_state"/"background").glob("*/task_state/inbox.sqlite3"))
    rows=[]
    for db in dbs:
        try:
            rows+=query(db,"SELECT provider,model,billing,role,created FROM usage_events WHERE kind='response' ORDER BY created DESC LIMIT 3")
        except sqlite3.Error:
            pass
    result["models"]={"recent_responses":sorted(rows,key=lambda r:r["created"],reverse=True)[:6],
                      "note":"Historical successes only; remaining subscription quota is not inferred."}
    if probe=="outlook":
        code="""import json,pythoncom,win32com.client
pythoncom.CoInitialize()
try:
 app=win32com.client.GetActiveObject('Outlook.Application')
 print(json.dumps({'status':'available' if app.Session.Accounts.Count else 'no_accounts','account_count':app.Session.Accounts.Count}))
except Exception:
 print(json.dumps({'status':'unavailable','note':'Classic Outlook not accessible as a running COM application. No window opened.'}))
finally:
 pythoncom.CoUninitialize()
"""
        try:
            run=subprocess.run([sys.executable,"-c",code],capture_output=True,timeout=12,encoding="utf-8",errors="replace",
                               creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            result["outlook"]=json.loads(run.stdout) if run.returncode==0 else {"status":"probe_error"}
        except (subprocess.TimeoutExpired,ValueError):
            result["outlook"]={"status":"timeout_or_error","note":"Do not assume account access."}
    path=root/"task_state"/"capabilities.json"
    with memory_lock(path):
        prior=read_json(path)
        if probe=="overview" and prior.get("outlook",{}).get("checked_at"):
            result["outlook"]=prior["outlook"]
        elif probe=="outlook":
            result["outlook"]["checked_at"]=now
        atomic_json(path,result)
    return result

def snapshot(root, tasks, chat):
    from agent_core.usage import aggregate, decode
    now=time.time()
    projects={}
    for kind in ("project","thesis","workflow","lesson"):
        try:
            projects[kind]=tasks.projects.manage(chat,"list",kind)
        except ValueError:
            projects[kind]=[]
    from supervisor import live_status
    states = live_status(Path(root).parent, now)['agents']
    states.pop('dashboard', None)
    # Calendar day in the user's timezone, not a rolling UTC window.
    midnight=datetime.now(ZoneInfo("UTC")).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
    groups={}
    dbs=[Path(root)/"task_state"/"inbox.sqlite3"]+list((Path(root)/"task_state"/"background").glob("*/task_state/inbox.sqlite3"))
    for db in dbs:
        try:
            events=query(db,"SELECT provider,model,billing,role,metrics FROM usage_events WHERE kind='response' AND created>=?",(midnight,))
            for e in events:
                key=(e["provider"],e["model"],e["billing"],e["role"])
                row=groups.setdefault(key,dict(provider=key[0],model=key[1],billing=key[2],role=key[3],calls=0,records=[]))
                row["calls"]+=1
                metrics=decode(e["metrics"], e["role"])
                row['records'].append(metrics)
        except (sqlite3.Error,ValueError):
            continue
    for row in groups.values():
        totals = aggregate(row.pop('records'))
        row.update(input_tokens=totals['input_total'], output_tokens=totals['output'],
                   reported=totals)
    reports=sorted((p for p in (Path(root)/"reports").glob("*") if p.is_file()),key=lambda p:p.stat().st_mtime,reverse=True)[:12]
    return {"updated":now,"agents":states,"capabilities":read_json(Path(root)/"task_state"/"capabilities.json"),
            "tasks":tasks.status(chat),"register":projects,"usage_today":list(groups.values()),
            "usage_note":"Recorded responses only; unknown usage stays flagged. Dollar cost, voice cost and remaining plan quota are not estimated.",
            "recent_files":[{"name":p.name,"path":str(p),"modified":p.stat().st_mtime,"verified":False} for p in reports],
            "recovery":"Ask "+Path(root).name.capitalize()+" to inspect task_evidence before resuming. Use /tasks, /resume <id>, /cancel <id>. The shared dashboard supports model and background-task controls."}

