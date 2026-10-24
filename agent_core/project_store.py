"""Versioned projects, theses and candidate lessons; local change-only alerts."""
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import time

class ProjectStore:
    def __init__(self, root):
        self.path = Path(root) / "task_state" / "projects.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS records(chat INTEGER,kind TEXT,key TEXT,version INTEGER,data TEXT,updated REAL,PRIMARY KEY(chat,kind,key))")
            db.execute("CREATE TABLE IF NOT EXISTS history(chat INTEGER,kind TEXT,key TEXT,version INTEGER,data TEXT,updated REAL)")
            db.execute("CREATE TABLE IF NOT EXISTS alerts(key TEXT PRIMARY KEY,sent REAL)")
    def connect(self):
        db=sqlite3.connect(self.path, timeout=15)
        db.row_factory=sqlite3.Row
        return db
    def manage(self, chat, action, kind="project", key="", data=None, version=0):
        if kind not in ("project","thesis","lesson","workflow"):
            raise ValueError("Use project, thesis, workflow or lesson.")
        with closing(self.connect()) as db, db:
            if action == "list":
                return [{"key":r["key"],"version":r["version"],"updated":r["updated"],
                         **{k:v for k,v in json.loads(r["data"]).items() if k in ("title","status","next_action","deadline")}}
                        for r in db.execute("SELECT * FROM records WHERE chat=? AND kind=? ORDER BY updated DESC LIMIT 40",(chat,kind))]
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}",key):
                raise ValueError("Use a short lowercase key.")
            row=db.execute("SELECT * FROM records WHERE chat=? AND kind=? AND key=?",(chat,kind,key)).fetchone()
            if action=="read":
                return {**dict(row),"data":json.loads(row["data"])} if row else {"error":"Not found"}
            if action=="history":
                return [{**dict(r),"data":json.loads(r["data"])} for r in db.execute(
                    "SELECT * FROM history WHERE chat=? AND kind=? AND key=? ORDER BY version DESC LIMIT 10",(chat,kind,key))]
            if action!="save" or not isinstance(data,dict):
                raise ValueError("Use list/read/history/save with a data object.")
            db.execute("BEGIN IMMEDIATE")
            row=db.execute("SELECT * FROM records WHERE chat=? AND kind=? AND key=?",(chat,kind,key)).fetchone()
            if version != (row["version"] if row else 0):
                raise ValueError("Version changed; reread before updating.")
            if not data.get("title"):
                raise ValueError("Title required.")
            required={"project":("goal","next_action","evidence","completion_criteria"),
                      "thesis":("claim","support","counterevidence","invalidators","catalysts","exposures"),
                      "lesson":("failure","source","candidate_change","checks","limitations"),
                      "workflow":("prerequisites","inputs","steps","checks","failure_recovery","scope","validation")}[kind]
            if any(k not in data for k in required):
                raise ValueError("Required fields: "+", ".join(required))
            if kind=="lesson" and data.get("status","candidate") not in ("candidate","rejected"):
                raise ValueError("Lessons remain candidates until an explicitly authorized deployment; no self-promotion.")
            if kind!="lesson" and data.get("status","active") not in ("active","blocked","paused","complete","invalidated"):
                raise ValueError("Unsupported status.")
            if data.get("deadline"):
                dt=datetime.fromisoformat(data["deadline"])
                if dt.tzinfo is None:
                    raise ValueError("Deadline must include timezone offset.")
            data.setdefault("status","candidate" if kind=="lesson" else "active")
            encoded=json.dumps(data,ensure_ascii=False)
            if len(encoded)>40000:
                raise ValueError("Keep records compact; link larger evidence files.")
            now=time.time()
            if row:
                db.execute("INSERT INTO history VALUES(?,?,?,?,?,?)",tuple(row))
            db.execute("INSERT OR REPLACE INTO records VALUES(?,?,?,?,?,?)",(chat,kind,key,version+1,encoded,now))
            return {"key":key,"version":version+1,"status":data["status"]}
    def notify_once(self, key, chat, message, send):
        with closing(self.connect()) as db:
            if db.execute("SELECT 1 FROM alerts WHERE key=?",(key,)).fetchone():
                return
        send(chat,message)
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR IGNORE INTO alerts VALUES(?,?)",(key,time.time()))
    def monitor(self, send, tasks):
        now=time.time()
        with closing(self.connect()) as db:
            rows=db.execute("SELECT * FROM records WHERE kind IN ('project','thesis')").fetchall()
        for row in rows:
            data=json.loads(row["data"])
            if data.get("status") not in ("active","blocked") or not data.get("deadline"):
                continue
            due=datetime.fromisoformat(data["deadline"]).timestamp()
            if due-now <= 86400:
                stage="overdue" if due<now else "due-soon"
                self.notify_once(f"due:{row['chat']}:{row['kind']}:{row['key']}:{data['deadline']}:{stage}",
                    row["chat"],f"{data['title']}: {stage}. Next action: {data.get('next_action','review the thesis')}. "
                    f"Deadline: {data['deadline']}.",send)
        with tasks.db() as db:
            active=[dict(r) for r in db.execute("SELECT * FROM tasks WHERE status='running'")]
        for task in active:
            path=tasks.directory(task["id"])/"task_state"/"health.json"
            if not path.exists():
                continue
            try:
                health=json.loads(path.read_text(encoding="utf-8"))
            except (OSError,ValueError):
                continue
            if now-float(health.get("last_progress",now))>600:
                self.notify_once(f"stalled:{task['id']}:{task['pid']}",task["chat"],
                    f"{tasks.label(task)} has reported no progress for over 10 minutes. Last stage: "
                    f"{health.get('phase','unknown')}. It needs inspection; I have not repeated any action.",send)
