"""Shared background runtime for all four agents (converged 2026-10-07 from four per-agent copies).

One background worker per agent plus the normal foreground chat; durable SQLite controls. Roles are
declared in agent_core.roster and passed to TaskStore: Cato owns Cal/Vit requests (delegation parking,
delivery gate, scorecard), Cal and Vit run delegate requests under the caller's cap and notify through
the request tables, Tacitus runs his own tasks. Workers are started as `python background_runtime.py --worker <id> --agent <dir> --role <name>`."""
import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from agent_core.worker_lease import WorkerLease, busy as lease_busy
from contextlib import contextmanager

# This file lives in the shared Agents/ directory; make sure it is importable when run as a worker.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_runtime import atomic_json
from agent_core.roster import ROLES
from agent_core import lifecycle
from agent_core.outcome import Refused

# The read_only task mode (tool whitelist + keyword gate on the brief + two automatic retries) was removed on
# 2026-10-07: in 90 tasks it was used 7 times, its automatic recovery never fired, and its keyword gate killed a
# request. Crash recovery now decides from the action records of the task itself (retry_interrupted).
import re

ACTIVE = ("queued", "starting", "running", "cancelling")
TERMINAL = ("finished", "cancelled", "superseded")       # cancel on these is a no-op, said so in the summary
# A worker whose delivery waits on an in-flight Cal/Vit request parks here. It holds no worker slot,
# so queued tasks can run; the monitor polls delegate status (no model tokens) and requeues it.
WAITING = "waiting_on_delegate"
DELEGATE_POLL_SECONDS = 90
# Measured 2026-10-06 over 19 real Cal/Vit jobs (reports/task_cost_history_2026-10-06.md): median 4 min,
# p90 7 min, longest 26 min. 2 x p90 (14 min) would cut off the longest real job, so 45 min covers every
# observed job with margin while still flagging a stuck delegate the same evening, not three hours later.
DELEGATE_MAX_WAIT_SECONDS = 45 * 60
# "capped": the delegate paused at its budget with a partial result; Cato reads it and decides on an extension.
REMOTE_RETURNED = ("completed", "partial", "blocked", "timed_out", "cancelled", "capped", "infeasible")
# Work the runtime starts about the agent's work (a correction diagnosis, a lesson trial, an eval rerun). It runs in
# its own lane so it never holds the slot ahead of Alex's own tasks (2026-10-21: a diagnosis blocked a Tiro fix
# for half an hour), and it is not precedent for a skill's track record.
META_PREFIXES = ("[Correction diagnosis]", "[Trial ", "[Eval ")


def meta(request):
    return str(request or "").startswith(META_PREFIXES)


NEEDS_ANSWER = "needs_answer"          # a delegate stopped on a question for Cato (2026-10-08); resume carries the answer
QUESTION_PREFIX = "QUESTION:"
# While a delegate is in flight a worker must park, not burn model turns sleeping/polling.
# Lesson: a task looped execute_command 'Start-Sleep 55; read Cal health.json', ~55k tokens per turn.
IN_FLIGHT_REFUSAL = Refused("A delegate is in flight. Save task_checkpoint and end your turn; "
                            "the runtime parks you and resumes when the result returns.")
# Only actual wait commands: a sleep/ping *command with a duration*, not any token containing the word
# (task 70 found 'sleep_study.py' and 'ping_check' refused). Relative '..\cal\task_state' is caught too.
WAIT_COMMAND_RE = re.compile(r"(?:^|[;&|(]\s*|\bstart-)sleep\b\s*(?:-s(?:econds)?\s+)?\d|\btimeout(?:\.exe)?\s+/t\b|"
                             r"\bwait-\w+|\bping(?:\.exe)?\s+-(?:n|t|c)\b|\btime\.sleep\(|\bwaitforexit\b|"
                             r"thread\]::sleep|delegate_local\.py\b.*\bstatus\b", re.IGNORECASE)
OTHER_AGENT_RE = re.compile(r"agents[\\/]+(?!cato\b)\w|\.\.[\\/]+(?!cato\b)\w+[\\/]", re.IGNORECASE)
STATE_FILE_RE = re.compile(r"task_state|checkpoint|health\.json", re.IGNORECASE)
STATUS_REPEAT_SECONDS = 300

class TaskStopped(Exception):
    pass

STALL_GRACE = 300

def worker_stalled(health, now, grace=STALL_GRACE):
    """True when a worker's health file shows a deadline passed by more than `grace` seconds (and a pid to stop)."""
    deadline = health.get("deadline")
    return bool(health.get("pid")) and bool(deadline) and now > float(deadline) + grace

REPEAT_NOTE = ("This lesson restates candidate {key}. The same lesson twice means the method needs changing, not another note: "
               "draft the skill edit and show it to Alex (lesson action=propose); his go applies it (lesson action=approve). "
               "Until then it stays a candidate.")

class TaskStore:
    def __init__(self, agent_dir, role=ROLES["cato"]):
        """role: the agent's Role from agent_core.roster. Callers pass it; the default (Cato's) is for tests on temp folders."""
        self.agent_dir = Path(agent_dir)
        self.role = role
        self.monitoring = False
        self.answering = None           # set by the agent: () -> the Telegram message id the running turn answers
        self.root = self.agent_dir / "task_state" / "background"
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "jobs.sqlite3"
        self.processes = {}
        self.worker_attempt = None
        self.last_monitor = 0
        self.last_delegate_poll = 0
        self.delegate_poll_thread = None
        self.status_calls = {}  # (task, request_id) -> time of last allowed delegate status call
        self.status_checker = self.remote_delegate_status
        from agent_core.project_store import ProjectStore
        self.projects = ProjectStore(self.agent_dir)
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY, chat INTEGER, request TEXT, status TEXT, created REAL, updated REAL, pid INTEGER, note TEXT, notified INTEGER DEFAULT 0)")
            task_columns = {r[1] for r in db.execute("PRAGMA table_info(tasks)")}
            if "attempt" not in task_columns:
                db.execute("ALTER TABLE tasks ADD COLUMN attempt TEXT")
            if "release" not in task_columns:                    # the Git commit the worker ran (agent_core.release)
                db.execute("ALTER TABLE tasks ADD COLUMN release TEXT")
            db.execute("CREATE TABLE IF NOT EXISTS worker_failures(task INTEGER PRIMARY KEY, note TEXT, created REAL, notified INTEGER DEFAULT 0)")
            # Recover failures hidden by the old delegate-success notification policy, once.
            db.execute("INSERT OR IGNORE INTO worker_failures(task,note,created) SELECT id,note,updated FROM tasks WHERE status='interrupted'")
            db.execute("CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY, task INTEGER NOT NULL, chat INTEGER NOT NULL, question TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', answer TEXT, source TEXT, control INTEGER, created REAL NOT NULL, updated REAL NOT NULL)")
            decision_columns = {r[1] for r in db.execute("PRAGMA table_info(decisions)")}
            # 2026-10-21: one record for everything Alex is asked. kind 'action' is a parked tool call (the exact tool
            # and inputs) that runs only on his yes, replacing the 60-second in-memory confirmation; message_ids are the
            # Telegram messages that asked it, so his reply to one of them answers it directly.
            for column in ("kind TEXT NOT NULL DEFAULT 'question'", "action TEXT", "message_ids TEXT"):
                if column.split()[0] not in decision_columns:
                    db.execute("ALTER TABLE decisions ADD COLUMN " + column)
            db.execute("DROP INDEX IF EXISTS one_pending_decision")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_pending_question ON decisions(task,chat) "
                       "WHERE state='pending' AND kind='question'")
            db.execute("CREATE TABLE IF NOT EXISTS controls(id INTEGER PRIMARY KEY, task INTEGER, text TEXT, applied INTEGER DEFAULT 0)")
            # Accepted activations (start, resume, reopen): what a turn's "work started" claim is checked against.
            db.execute("CREATE TABLE IF NOT EXISTS activations(id INTEGER PRIMARY KEY, task INTEGER, chat INTEGER, action TEXT, created REAL)")
            db.execute("CREATE TABLE IF NOT EXISTS delegations(task INTEGER, request_id TEXT, agent TEXT, purpose TEXT, source TEXT, status TEXT, created REAL, updated REAL, PRIMARY KEY(task, request_id))")
            # Durable status-call timer: a resumed worker used to get one free status poll per restart.
            columns = {r[1] for r in db.execute("PRAGMA table_info(delegations)")}
            if "last_status" not in columns:
                db.execute("ALTER TABLE delegations ADD COLUMN last_status REAL")
            if "acceptance" not in columns:
                db.execute("ALTER TABLE delegations ADD COLUMN acceptance TEXT")
            db.execute("CREATE TABLE IF NOT EXISTS requirements(task INTEGER NOT NULL, request TEXT NOT NULL DEFAULT '', items TEXT NOT NULL, PRIMARY KEY(task,request))")
            # Outcome ledger per delegate request: what was asked, what it cost, what was verified as met.
            db.execute("CREATE TABLE IF NOT EXISTS scorecard(request_id TEXT PRIMARY KEY, task INTEGER, agent TEXT, task_type TEXT, "
                       "budget INTEGER, items TEXT, spent INTEGER, items_total INTEGER, items_met INTEGER, critical_total INTEGER, "
                       "critical_met INTEGER, reworks INTEGER DEFAULT 0, verdicts TEXT, created REAL, accepted REAL, brief TEXT, brief_lesson TEXT)")
            score_columns = {r[1] for r in db.execute("PRAGMA table_info(scorecard)")}
            for column in ("brief TEXT", "brief_lesson TEXT", "reason TEXT", "direct_cost INTEGER", "guesses TEXT", "guesses_verdict TEXT"):
                if column.split()[0] not in score_columns:
                    db.execute("ALTER TABLE scorecard ADD COLUMN " + column)

        from agent_core.task_decisions import migrate_questions
        migrate_questions(self)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def label(task):
        text = " ".join(task.get("request", "").splitlines()[0].split()) if task.get("request") else ""
        return text[:100] + ("…" if len(text) > 100 else "") if text else "Background work"

    def resolve(self, chat, reference):
        with self.db() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM tasks WHERE chat=? ORDER BY id DESC", (chat,))]
        if reference.isdecimal():
            matches = [row for row in rows if row["id"] == int(reference)]
        else:
            key = reference.casefold().strip()
            matches = [row for row in rows if key == self.label(row).casefold()
                       or key == row["request"].casefold().strip()]
            if not matches and key:
                matches = [row for row in rows if key in row["request"].casefold()]
        if not matches:
            raise ValueError("No matching task. Use /tasks to see the descriptions.")
        if len(matches) != 1:
            raise ValueError("Several tasks match. Please give a more specific description.")
        return matches[0]["id"]

    def directory(self, ident):
        return self.root / str(int(ident))

    def get(self, ident):
        with self.db() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (int(ident),)).fetchone()
        if not row:
            raise ValueError("Unknown background task.")
        return dict(row)

    def move(self, ident, op, status, note="", quiet=False):
        """The one way this store changes a task's status outside a larger transaction: operation `op` of
        agent_core.lifecycle, refused if the table does not allow it (2026-10-21). Returns True when made."""
        with self.db() as db:
            if self.worker_attempt:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("SELECT attempt FROM tasks WHERE id=?", (int(ident),)).fetchone()
                if not current or current[0] != self.worker_attempt:
                    raise RuntimeError("Worker no longer owns this task attempt")
            # A delegate request (Cal/Vit) reports through its request table, not a Telegram notice.
            local = False
            if self.role.request_table:
                mapped = db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (self.role.request_table,)).fetchone()
                local = bool(mapped and db.execute(f"SELECT 1 FROM {self.role.request_table} WHERE task=?", (int(ident),)).fetchone())
            made = lifecycle.move(db, ident, op, status, note, notified=1 if local else 0, quiet=quiet)
            if made and status == "interrupted":
                db.execute("INSERT OR IGNORE INTO worker_failures(task,note,created) VALUES (?,?,?)",
                           (int(ident), note, time.time()))
            return made

    def start(self, chat, request, context="", extra=None, parked_on=None, completion_items=None):
        """parked_on={'agent','request_id','purpose'}: the task is born waiting on that delegate (2026-10-21). A
        foreground hand-off to Cal or Vit used to need a Cato task to RUN first, just to submit and park; with one
        worker slot busy, the hand-off itself queued behind unrelated work (Tiro, task 264). Now the delegate
        starts at once and the owner waits without a slot until poll_waiting wakes it with the result."""
        if completion_items is not None:
            from agent_core.verification import validate_items
            validate_items(completion_items)
            if any(it.get("source", 0) != 0 for it in completion_items):
                raise ValueError("Initial requirements belong to source 0, the original brief.")
        if not request.strip():
            raise ValueError("A task needs an explicit scope.")
        if parked_on:
            now = time.time()
            with self.db() as db:
                db.execute("BEGIN IMMEDIATE")
                cursor = db.execute("INSERT INTO tasks(chat,request,status,created,updated,note,notified) VALUES (?,?,?,?,?,?,1)",
                                    (chat, request, WAITING, now, now, f"Waiting on delegate: {parked_on['agent']} {parked_on['request_id']}"))
                ident = cursor.lastrowid
                db.execute("INSERT INTO requirements(task,request,items) VALUES (?,'',?)",
                           (ident, json.dumps(completion_items or [], ensure_ascii=False)))
                db.execute("INSERT INTO activations(task,chat,action,created) VALUES (?,?,?,?)", (ident, chat, "start", now))
                self.directory(ident).mkdir(exist_ok=True)
                atomic_json(self.directory(ident) / "context.json", {"context": context, "request": request, "retries": 0,
                                                                     "parked_at": now, **(extra or {})})
            self.add_delegation(ident, parked_on["agent"], parked_on["request_id"], parked_on.get("purpose", ""), "owner_decision")
            task = self.get(ident)
            task["summary"] = (f"task {ident} waits on {parked_on['agent']} request {parked_on['request_id']} without holding a "
                               "worker slot; it resumes by itself when the result returns")
            return task
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM tasks WHERE chat=? AND request=? AND status IN ('queued','starting','running','cancelling','waiting_on_delegate')", (chat, request)).fetchone()
            if prior:
                return dict(prior)
            cursor = db.execute("INSERT INTO tasks(chat,request,status,created,updated,note) VALUES (?,?,?,?,?,?)",
                                (chat, request, "queued", time.time(), time.time(), "Waiting for background worker"))
            ident = cursor.lastrowid
            db.execute("INSERT INTO requirements(task,request,items) VALUES (?,'',?)",
                       (ident, json.dumps(completion_items or [], ensure_ascii=False)))
            db.execute("INSERT INTO activations(task,chat,action,created) VALUES (?,?,?,?)", (ident, chat, "start", time.time()))
            self.directory(ident).mkdir(exist_ok=True)
            extra = dict(extra or {})
            if self.answering and not extra.get("message_id"):
                extra["message_id"] = self.answering()         # the result quotes this message if Alex has moved on
            atomic_json(self.directory(ident) / "context.json", {"context": context, "request": request, "retries": 0, **extra})
        # Start it now when a slot is free, so the reply describes what happened rather than what will (Alex,
        # 2026-10-11: "when a person says he is about to do something, he does it right after saying it").
        started = self.monitoring and self.launch_next() == ident      # only while this process serves the queue
        task = self.get(ident)
        if started:
            task["summary"] = f"Launch requested for task {ident}; use live status to confirm the worker is running."
        else:
            with self.db() as db:
                ahead = [r[0] for r in db.execute("SELECT id FROM tasks WHERE status IN ('starting','running','cancelling') AND id<>? ORDER BY id", (ident,))]
            task["summary"] = (f"queued: task {ident} waits behind task {', '.join(map(str, ahead))} and starts automatically when a slot frees"
                               if ahead else f"queued: task {ident} will start at the next monitor tick")
        return task

    def control(self, chat, ident, action, text=""):
        task = self.get(ident)
        if task["chat"] != chat:
            raise ValueError("Task belongs to another chat.")
        with self.db() as db:
            if action == "steer":
                if not text.strip():
                    raise ValueError("Supply the changed scope or instruction.")
                db.execute("INSERT INTO controls(task,text) VALUES (?,?)", (int(ident), text))
            elif action == "cancel":
                db.execute("UPDATE decisions SET state='cancelled',updated=? WHERE task=? AND state='pending'", (time.time(), int(ident)))
                # Cancel means "I no longer want this", whatever state it is in (2026-10-12). Before, a task that had
                # already stopped (needs_attention, interrupted, needs_answer, capped, infeasible) was returned
                # unchanged without an error: Cato told Alex "killed" twice while task 143 stayed open and was
                # injected into every message as live background work, so he kept asking about it.
                if task["status"] in TERMINAL:
                    task["summary"] = f"nothing to cancel: task {ident} is already {task['status']}"
                    return task
                if task["status"] in ("running", "starting"):
                    lifecycle.move(db, ident, "cancel", "cancelling")
                else:
                    note = ("Cancelled while waiting on a delegate." if task["status"] == WAITING
                            else f"Cancelled by request (was {task['status']}).")
                    lifecycle.move(db, ident, "cancel", "cancelled", note)
            elif action == "resume":
                if task["status"] == "superseded":
                    raise ValueError("This task was superseded. Follow its recorded successor: " + task["note"])
                if db.execute("SELECT 1 FROM decisions WHERE task=? AND state='pending'", (int(ident),)).fetchone():
                    raise ValueError("Task awaits a recorded decision. Use task_decision answer with the user's source quote.")
                if task["status"] in ACTIVE:
                    raise ValueError("Task is already active.")
                # A finished task gets more work only by reopening it, by name (2026-10-21).
                op = "reopen" if task["status"] == "finished" else "resume"
                lifecycle.move(db, ident, op, "queued", ("Reopened" if op == "reopen" else "Resume queued")
                               + "; waiting for previous worker ownership to release")
                db.execute("DELETE FROM worker_failures WHERE task=?", (int(ident),))
                db.execute("INSERT INTO activations(task,chat,action,created) VALUES (?,?,?,?)", (int(ident), chat, op, time.time()))
                if text:
                    db.execute("INSERT INTO controls(task,text) VALUES (?,?)", (int(ident), text))
            else:
                raise ValueError("Use steer, cancel or resume.")
        return self.get(ident)


    def pending_controls(self, ident):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT id,text FROM controls WHERE task=? AND applied=0 ORDER BY id", (int(ident),))]

    def acknowledge(self, ids):
        with self.db() as db:
            db.executemany("UPDATE controls SET applied=1 WHERE id=?", [(i,) for i in ids])
            db.executemany("UPDATE decisions SET state='delivered',updated=? WHERE control=? AND state='answered'", [(time.time(), i) for i in ids])

    def scope_updates(self, ident):
        """Instructions added to a task after it started (steer and resume texts) that its worker has received."""
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT id,text FROM controls WHERE task=? AND applied=1 ORDER BY id", (int(ident),))]

    def preset_case(self, ident):
        from agent_core import regression
        path = self.directory(ident) / "context.json"
        context = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return regression.bound_case(self.agent_dir, context)

    def contract(self, ident):
        """The task's contract: its brief plus every instruction added since, as one block of text (2026-10-12).
        Long workers only see the last 30-50 messages; on 2026-10-11 task 144 lost its brief at message 52 and
        Alex's added carrier basket at message 73, then delivered towers only. The engine pins this block into
        every trimmed request and the finish check sets it next to the answer."""
        task = self.get(ident)
        brief = task["request"]
        lines = ["[Task contract: the brief and every instruction added later. Older messages are trimmed from your "
                 "view; this block is not. Your final answer must deliver all of it, or say plainly what was not done and why.]",
                 "Brief:", brief]
        for update in self.scope_updates(ident):
            text = update["text"]
            lines += ["", f"Added instruction (scope update #{update['id']}):", text]
        checkpoint = self.directory(ident) / "checkpoint.json"
        if checkpoint.exists():
            value = json.loads(checkpoint.read_text(encoding="utf-8"))
            current = {k: value[k] for k in ("completed", "next_action", "evidence", "blocker", "facts", "guesses", "decisions") if value.get(k)}
            if current:
                lines += ["Current checkpoint (agent record, not new user authorization):", json.dumps(current, ensure_ascii=False)]
        from agent_core import verification
        case = self.preset_case(ident)
        if case is not None:
            lines += ["Preset acceptance criteria: the runtime checks the delivered files against this fixed contract. "
                      "Do not create a second checkpoint checklist or duplicate its verification calls.",
                      json.dumps([dict(id=r["id"], item=r["description"]) for r in case["requirements"]], ensure_ascii=False)]
            return "\n".join(lines)
        items = verification.saved_items(self, ident)
        lines += ["Durable requirements (source 0 = brief; source N = added instruction N). Checkpoints merge, never remove. "
                  "Declare obligations for every instruction, including constraints. Verify failed or unverified items; passed proof remains valid until its requirement, file or dependency changes.",
                  json.dumps([dict(it, id=n, status=verification.status(self, ident, "", n)) for n, it in enumerate(items, 1)], ensure_ascii=False)]
        missing = verification.uncovered_sources(self, ident, items)
        if missing:
            lines.append("Instructions without declared requirements: " + str(missing))
        return "\n".join(lines)

    # ── Pending delegations (Cal/Vit requests attached to a background task) ──
    DELEGATION_RANK = {"submitted": 0, "returned": 1, "read": 2, "cancelled": 2}

    def add_delegation(self, ident, agent, request_id, purpose, source):
        if source not in ("owner_decision", "worker_judgment"):
            raise ValueError("source must be owner_decision or worker_judgment")
        now = time.time()
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO delegations(task,request_id,agent,purpose,source,status,created,updated) VALUES (?,?,?,?,?,?,?,?)",
                       (int(ident), request_id, agent, purpose, source, "submitted", now, now))

    def delegations(self, ident):
        with self.db() as db:
            return [dict(r) for r in db.execute(
                "SELECT agent,request_id,purpose,source,status,updated,acceptance FROM delegations WHERE task=? ORDER BY created",
                (int(ident),))]

    def command_refusal(self, ident, command):
        """IN_FLIGHT_REFUSAL if a delegate is in flight and the command sleeps/waits or reads
        another agent's task_state/checkpoint/health files; else None. Local DB only, no remote call."""
        if not any(d["status"] == "submitted" for d in self.delegations(ident)):
            return None
        if WAIT_COMMAND_RE.search(command or "") or (
                OTHER_AGENT_RE.search(command or "") and STATE_FILE_RE.search(command or "")):
            return IN_FLIGHT_REFUSAL
        return None

    def path_refusal(self, ident, path):
        """IN_FLIGHT_REFUSAL when read_file targets another agent's task_state/checkpoint/health while a
        delegate is in flight. Relative paths resolve against the worker's cwd (the agent directory), so
        '..\\callimachus\\task_state\\...' is caught; the worker's own files and project files stay readable."""
        if not any(d["status"] == "submitted" for d in self.delegations(ident)):
            return None
        text = str(path or "")
        try:
            resolved = str((Path(text) if Path(text).is_absolute() else self.agent_dir / text).resolve())
        except (OSError, ValueError):
            resolved = text
        probe = text + "\n" + resolved   # the raw '..\cal\...' form and where it actually points
        if OTHER_AGENT_RE.search(probe) and STATE_FILE_RE.search(probe):
            return IN_FLIGHT_REFUSAL
        return None

    def status_refusal(self, ident, request_id, now=None):
        """IN_FLIGHT_REFUSAL for a repeated status call on the same request within STATUS_REPEAT_SECONDS.
        The timer lives in the delegations row, so it survives a worker restart."""
        now = time.time() if now is None else now
        key = (int(ident), request_id)
        with self.db() as db:
            row = db.execute("SELECT last_status FROM delegations WHERE task=? AND request_id=?", key).fetchone()
            last = row["last_status"] if row and row["last_status"] is not None else self.status_calls.get(key)
            if last is not None and now - last < STATUS_REPEAT_SECONDS:
                return IN_FLIGHT_REFUSAL
            db.execute("UPDATE delegations SET last_status=? WHERE task=? AND request_id=?", (now, *key))
        self.status_calls[key] = now
        return None

    def delegation_tasks(self, request_id):
        with self.db() as db:
            return [r[0] for r in db.execute("SELECT task FROM delegations WHERE request_id=?", (request_id,))]

    def delegation_returned_at(self, request_id, ident=0):
        """When the request's result was recorded as returned (the delegation row's update time once past submitted);
        None when nothing is recorded for it (a request submitted outside the register, or still in flight)."""
        with self.db() as db:
            q, args = "SELECT updated FROM delegations WHERE request_id=? AND status IN ('returned','read')", [request_id]
            if ident:
                q += " AND task=?"; args.append(int(ident))
            rows = db.execute(q + " ORDER BY updated", args).fetchall()
        return rows[0][0] if rows else None

    ACCOUNT_LINES = ("DONE:", "OPEN:", "EVIDENCE:", "COST:")

    def save_delegate_result(self, ident, request_id, text):
        """The delegate's result text as fetched, under the owner task (delegate_results/<request_id>.txt)."""
        import re
        from agent_core.memory_store import atomic_text
        folder = self.directory(ident) / "delegate_results"
        folder.mkdir(parents=True, exist_ok=True)
        atomic_text(folder / (re.sub(r"[^A-Za-z0-9_.-]", "_", request_id) + ".txt"), text or "")

    def delegate_account(self, ident):
        """The delegate's own head lines (DONE / OPEN / EVIDENCE / COST), unedited, for the message to Alex
        (2026-10-24, item 2 of the plan: Tiro was called ready three times on Cato's reading of Vit's reports; now
        Vit's account travels with the verdict, so Alex reads what Vit actually claimed and what he left open)."""
        import re
        parts = []
        for d in self.delegations(ident):
            if d.get("status") not in ("returned", "read"):
                continue
            path = self.directory(ident) / "delegate_results" / (re.sub(r"[^A-Za-z0-9_.-]", "_", d["request_id"]) + ".txt")
            if not path.exists():
                continue
            lines = []
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith(self.ACCOUNT_LINES):
                    lines.append(line.strip()[:400])
                elif lines:
                    break                                       # the head block ends at the first other line
            who = {"vit": "Vit", "cal": "Cal"}.get(str(d.get("agent", "")).lower(), str(d.get("agent", "")))
            if lines:
                parts.append(f"{who}'s own account ({d.get('purpose', '')}), unedited:\n" + "\n".join(lines))
            else:
                parts.append(f"{who}'s result for {d.get('purpose', '')} carried no DONE / OPEN / EVIDENCE / COST lines; only Cato's reading above.")
        return "\n\n".join(parts)

    def unrun_line(self, ident):
        """Current requirement status, including stale evidence and report-only results."""
        from agent_core import verification
        lines = [verification.summary(self, ident)]
        for delegation in self.delegations(ident):
            if delegation["status"] == "read":
                lines.append(verification.summary(self, ident, delegation["request_id"]))
        return "\n".join(line for line in lines if line)

    def mark_delegation(self, ident, request_id, status):
        """Advance status; never moves a read/cancelled entry backwards."""
        with self.db() as db:
            row = db.execute("SELECT status FROM delegations WHERE task=? AND request_id=?",
                             (int(ident), request_id)).fetchone()
            if not row or self.DELEGATION_RANK[status] <= self.DELEGATION_RANK.get(row["status"], 0):
                return row["status"] if row else None
            db.execute("UPDATE delegations SET status=?,updated=? WHERE task=? AND request_id=?",
                       (status, time.time(), int(ident), request_id))
        return status

    # ── Acceptance: a result counts as read only after a verdict per completion item ──
    VERDICTS = ("met", "partial", "not_met")

    def record_submission(self, request_id, task, agent, task_type, budget, items, brief=None, reason="", direct_cost=0):
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO scorecard(request_id,task,agent,task_type,budget,items,created,brief,reason,direct_cost) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (request_id, int(task or 0), agent, task_type, int(budget or 0), None, time.time(),
                        brief if isinstance(brief, str) or brief is None else json.dumps(brief, ensure_ascii=False), reason, int(direct_cost or 0)))
            db.execute("INSERT OR IGNORE INTO requirements(task,request,items) VALUES (?,?,?)",
                       (int(task or 0), request_id, json.dumps(items, ensure_ascii=False)))

    def record_guesses(self, request_id, guesses):
        """The delegate's stated assumptions, copied from its checkpoint when Cato fetches status or result."""
        with self.db() as db:
            db.execute("UPDATE scorecard SET guesses=? WHERE request_id=?", (json.dumps(list(guesses), ensure_ascii=False), request_id))

    def guesses(self, request_id):
        with self.db() as db:
            row = db.execute("SELECT guesses FROM scorecard WHERE request_id=?", (request_id,)).fetchone()
        try:
            return json.loads(row["guesses"]) if row and row["guesses"] else []
        except (ValueError, TypeError):
            return []

    def record_guesses_verdict(self, request_id, verdict):
        with self.db() as db:
            db.execute("UPDATE scorecard SET guesses_verdict=? WHERE request_id=?", (verdict, request_id))

    def record_lesson(self, request_id, lesson):
        with self.db() as db:
            db.execute("UPDATE scorecard SET brief_lesson=? WHERE request_id=?", (lesson, request_id))

    def contract_items(self, request_id):
        from agent_core import verification
        card = self.scorecard(request_id)
        return verification.saved_items(self, int(card["task"]), request_id) if card else []

    def bump_rework(self, request_id, items=None, count=True):
        from agent_core import verification
        if items is not None:
            card = self.scorecard(request_id)
            verification.declare(self, int(card["task"]), items, request_id)
        db = sqlite3.connect(self.database, timeout=30)
        try:
            if count:
                db.execute("UPDATE scorecard SET reworks=COALESCE(reworks,0)+1 WHERE request_id=?", (request_id,))
            db.commit()
        finally:
            db.close()

    def accept_delegation(self, ident, request_id, verdicts, spent=None):
        """Store the per-item verdicts (each with evidence), mark the request read, update the scorecard.
        Returns (critical_unmet, unmet) item numbers so the caller can decide on rework."""
        from agent_core import verification
        verdicts = verification.verdicts(self, ident, request_id, verdicts)
        items = self.contract_items(request_id)
        by_number = {int(v["item"]): v for v in verdicts}
        unmet = [n for n in range(1, len(items) + 1) if by_number.get(n, {}).get("verdict") != "met"]
        critical_unmet = [n for n in unmet if n - 1 < len(items) and items[n - 1].get("critical")]
        met = len(items) - len(unmet)
        critical_total = sum(1 for it in items if it.get("critical"))
        with self.db() as db:
            if ident:
                db.execute("UPDATE delegations SET acceptance=?,updated=? WHERE task=? AND request_id=?",
                           (json.dumps(verdicts, ensure_ascii=False), time.time(), int(ident), request_id))
            db.execute("UPDATE scorecard SET spent=?, items_total=?, items_met=?, critical_total=?, critical_met=?, verdicts=?, accepted=? WHERE request_id=?",
                       (spent, len(items), met, critical_total, critical_total - len(critical_unmet), json.dumps(verdicts, ensure_ascii=False), time.time(), request_id))
        if ident:
            self.mark_delegation(ident, request_id, "read")
        return critical_unmet, unmet

    def scorecard(self, request_id=None):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            query = ("SELECT s.*,r.items AS durable_items FROM scorecard s LEFT JOIN requirements r "
                     "ON r.task=s.task AND r.request=s.request_id")
            rows = db.execute(query + (" WHERE s.request_id=?" if request_id else " ORDER BY s.created"),
                              (request_id,) if request_id else ()).fetchall()
            result = []
            for row in rows:
                card = dict(row)
                saved = card.pop("durable_items")
                if saved is not None:
                    card["items"] = saved
                result.append(card)
            return (result[0] if result else None) if request_id else result
        finally:
            db.close()

    def reopen_delegation(self, ident, request_id):
        """A capped delegate resumed after a budget extension is in flight again."""
        with self.db() as db:
            db.execute("UPDATE delegations SET status='submitted',updated=? WHERE task=? AND request_id=?",
                       (time.time(), int(ident), request_id))

    def unread_delegations(self, ident):
        return [d for d in self.delegations(ident) if d["status"] not in ("read", "cancelled")]

    def delivery_block(self, ident, final_text=None):
        """A dependency cannot be accepted or bypassed by prose in a final reply."""
        pending = self.unread_delegations(ident)
        if not pending:
            return None
        items = "; ".join(f"{d['agent']} {d['request_id']} ({d['purpose']}, {d['source']}, {d['status']})" for d in pending)
        return ("DELIVERY BLOCKED: these delegated requests are attached to this task and have not been accepted: " + items +
                ". Fetch each with delegate(action='result', agent=..., request_id=...), verify it, then record "
                "delegate(action='accept', agent=..., request_id=..., verdicts=[one per completion item with evidence]) before using it. "
                "If one is still running, do not deliver solo and do NOT wait for it yourself: no sleep/Start-Sleep/"
                "timeout commands, no repeated delegate status calls, no reading other agents' task_state, checkpoint "
                "or health files. Finish any independent work, save a task_checkpoint, then end your turn with a short "
                "interim note; the runtime parks the task (no model calls) and resumes you when the result returns. "
                "Unusable or unaccepted results remain incomplete; explain the blocker honestly.")

    # ── Waiting on a long-running delegate: park, poll cheaply, resume ──
    def remote_delegate_status(self, agent, request_id):
        """Remote status string via delegate_local.py (no model call); None if it cannot be read."""
        try:
            proc = subprocess.run([sys.executable, str(self.agent_dir / "scripts" / "delegate_local.py"), agent, "status",
                                   "--request-id", request_id], capture_output=True, encoding="utf-8", errors="replace",
                                  timeout=45, env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            data = json.loads(proc.stdout) if proc.returncode == 0 else {}
            if data.get("status") == "question":
                return {"status": "question", "question": data.get("question", "")}
            return data.get("status") or None
        except Exception:
            return None

    def decisions(self, chat, task=None):
        from agent_core.task_decisions import pending
        return pending(self, chat, task)

    def ask(self, chat, ident, question):
        from agent_core.task_decisions import ask
        return ask(self, chat, ident, question)

    def answer(self, chat, decision, answer, source_id, quote):
        from agent_core.task_decisions import answer_user
        return answer_user(self, chat, decision, answer, source_id, quote)

    def finish(self, ident, result):
        from agent_core.task_decisions import finish
        return finish(self, ident, result)

    def live(self, chat, ident):
        from agent_core.task_decisions import live
        return live(self, chat, ident)

    def live_context(self, chat, task=None):
        from agent_core.task_decisions import context
        return context(self, chat, task)

    def open_question(self, ident):
        rows = self.decisions(self.get(ident)["chat"], ident)
        if rows:
            return rows[0]["question"]
        return self.legacy_question(ident)

    def legacy_question(self, ident):
        """Import legacy questions only until a task has a decision ledger; answered questions stay answered."""
        db = sqlite3.connect(self.database, timeout=30)
        try:
            if db.execute("SELECT 1 FROM decisions WHERE task=?", (int(ident),)).fetchone():
                return ""
        finally:
            db.close()
        try:
            data = json.loads((self.directory(ident) / "checkpoint.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        blocker = str(data.get("blocker") or "").strip()
        return blocker[len(QUESTION_PREFIX):].strip() if blocker.startswith(QUESTION_PREFIX) else ""

    def refresh_delegations(self, ident):
        """Mark finished remote requests returned; return those still in flight. A delegate waiting on a question
        is relayed once to this task as a steer (the task is then woken by poll_waiting)."""
        in_flight = []
        for d in self.delegations(ident):
            if d["status"] != "submitted":
                continue
            remote = self.status_checker(d["agent"], d["request_id"])
            if remote in REMOTE_RETURNED:
                self.mark_delegation(ident, d["request_id"], "returned")
                continue
            if isinstance(remote, dict) and remote.get("status") == "question":
                self.relay_question(ident, d, remote.get("question", ""))
            in_flight.append(d)
        return in_flight

    def unanswered(self, ident, delegations):
        """True when one of these delegations' questions was relayed but no answer control has been applied since."""
        for d in delegations:
            with self.db() as db:
                row = db.execute("SELECT 1 FROM controls WHERE task=? AND applied=0 AND text LIKE ?",
                                 (int(ident), f"%{d['request_id']}%stopped on a question%")).fetchone()
            if row:
                return True
        return False

    def relay_question(self, ident, delegation, question):
        """Steer this task with the delegate's question, once per distinct question; returns True when relayed."""
        path = self.directory(ident) / "context.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        key = delegation["request_id"] + "\x00" + question
        seen = data.setdefault("questions_relayed", [])
        if not question or key in seen:
            return False
        seen.append(key)
        atomic_json(path, data)
        who = "Cal" if delegation["agent"] == "cal" else "Vit"
        task = self.get(ident)
        self.control(task["chat"], ident, "steer",
            f"{who} request {delegation['request_id']} ({delegation['purpose']}) has stopped on a question for you: {question}\n"
            f"Answer it with delegate(action='answer', agent='{delegation['agent']}', request_id='{delegation['request_id']}', "
            "note=<your answer>); the delegate resumes from its checkpoint. If you cannot answer, say so in the note and it will deliver what it has.")
        return True

    def poll_waiting(self):
        with self.db() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM tasks WHERE status=?", (WAITING,))]
        for task in rows:
            ident = task["id"]
            in_flight = self.refresh_delegations(ident)
            if self.get(ident)["status"] != WAITING:  # cancelled/resumed meanwhile
                continue
            asked = [d for d in in_flight if isinstance(self.status_checker(d["agent"], d["request_id"]), dict)]
            if asked and self.unanswered(ident, asked):
                with self.db() as db:
                    lifecycle.move(db, ident, "wake", "queued", "Delegate asked a question; resume queued", notified=1, quiet=True)
                continue
            if not in_flight:
                returned = [d for d in self.delegations(ident) if d["status"] == "returned"]
                for d in returned:
                    who = "Cal" if d["agent"] == "cal" else "Vit"
                    self.control(task["chat"], ident, "steer",
                        f"{who} request {d['request_id']} returned; read it with delegate(action='result', agent='{d['agent']}', "
                        f"request_id='{d['request_id']}') and use it ({d['purpose']}).")
                with self.db() as db:
                    lifecycle.move(db, ident, "wake", "queued", "Delegate returned; resume queued", notified=1, quiet=True)
                continue
            try:
                parked = json.loads((self.directory(ident) / "context.json").read_text(encoding="utf-8")).get("parked_at", task["updated"])
            except (OSError, ValueError):
                parked = task["updated"]
            if time.time() - parked > DELEGATE_MAX_WAIT_SECONDS:
                ids = "、".join(f"{'Cal' if d['agent'] == 'cal' else 'Vit'} {d['request_id']}" for d in in_flight)
                from agent_core.memory_store import atomic_text
                atomic_text(self.directory(ident) / "result.txt",
                    f"后台任务在等 {ids} 的结果，已超过 {DELEGATE_MAX_WAIT_SECONDS // 60} 分钟仍未返回，所以暂停了，没有交付。"
                    "可以让我继续等（resume），也可以取消，或者先去查一下对方那边的状态。")
                self.move(ident, "stop", "needs_attention", "Delegate wait exceeded; foreground decision needed.", quiet=True)

    def activated_since(self, chat, since):
        """True when this chat accepted a start, resume or reopen at or after `since` (a turn's start time). The
        turn's "work started" claim is checked against this; until 2026-10-21 it compared the highest task id, so
        a resumed or reopened task counted as nothing started (Cato's runtime report 2026-10-21-2242)."""
        with self.db() as db:
            return db.execute("SELECT 1 FROM activations WHERE chat=? AND created>=? LIMIT 1", (chat, since)).fetchone() is not None

    def newest(self, chat):
        """The highest task id for the chat, or 0."""
        with self.db() as db:
            return db.execute("SELECT COALESCE(MAX(id),0) FROM tasks WHERE chat=?", (chat,)).fetchone()[0]

    def status(self, chat):
        with self.db() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM tasks WHERE chat=? ORDER BY id DESC LIMIT 10", (chat,))]
        for row in rows:
            row["delegations"] = self.delegations(row["id"])
            if row["delegations"]:
                row["delegation_note"] = "Check these: unread requests block the worker's delivery."
            if row["status"] == WAITING:
                row["waiting_note"] = (f"Parked (no worker slot held) until the in-flight delegate returns; polled every "
                                       f"{DELEGATE_POLL_SECONDS}s, auto-resumes, needs_attention after {DELEGATE_MAX_WAIT_SECONDS // 60} min.")
            folder = self.directory(row["id"])
            result = folder / "result.txt"
            row["result_file"] = str(result) if result.exists() else None
            row["result_exists"] = result.exists()
            row["result_saved_at"] = result.stat().st_mtime if result.exists() else None
            row["worker_alive"] = self.lease_busy(row["id"]) if row["status"] in ("starting", "running", "cancelling") else False
            row["evidence_directory"] = str(folder / "task_state" / "actions")
            row["evidence_note"] = "Saved answer is not verified task completion; inspect action outcomes and artifacts."

            health = self.directory(row["id"]) / "task_state" / "health.json"
            if health.exists():
                try:
                    row["progress"] = json.loads(health.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    pass
        return rows

    def overview(self, chat):
        """What background_task(status) returns: one short row per recent task. The full rows from status() carry every
        brief in full (23 KB for ten tasks on 2026-10-12), and stale text inside an old brief came back as if it were
        current state. The brief, actions and result stay one call away in evidence()."""
        def when(ts):
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else None
        rows = []
        for row in self.status(chat):
            first = (row.get("request") or "").strip().splitlines()
            label = first[0] if first else ""
            item = {"id": row["id"], "status": row["status"],
                    "label": label[:100] + ("..." if len(label) > 100 else ""),
                    "created": when(row.get("created")), "updated": when(row.get("updated")),
                    "result_saved": bool(row.get("result_exists"))}
            live = self.live(chat, row["id"])
            item["observed_state"] = live["state"]
            item["worker_alive"] = live["worker_owned"]
            item["heartbeat_current"] = live["heartbeat_current"]
            if row.get("note"):
                item["note"] = str(row["note"])[:160]
            for key in ("delegation_note", "waiting_note"):
                if row.get(key):
                    item[key] = row[key]
            rows.append(item)
        return {"tasks": rows, "detail": "task_evidence(id) for a task's full brief, actions and result."}

    def evidence(self, chat, ident, limit=8):
        task = self.get(ident)
        if task["chat"] != chat:
            raise ValueError("Task belongs to another chat.")
        folder = self.directory(ident)
        counts, recent = {}, []
        paths = sorted((folder / "task_state" / "actions").glob("*.json"), reverse=True)
        for path in paths:
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            status = item.get("status", "unknown")
            counts[status] = counts.get(status, 0) + 1
            if len(recent) < min(20, max(1, int(limit))):
                recent.append({"tool": item.get("tool"), "status": status, "record": str(path),
                               "saved_at": path.stat().st_mtime})
        task.update(worker_alive=self.lease_busy(ident) if task["status"] in ACTIVE else False,
                    action_counts=counts, recent_actions=recent,
                    result_file=str(folder / "result.txt") if (folder / "result.txt").exists() else None,
                    interpretation="finished action means the tool returned, not that its requested outcome succeeded. Read the record and verify artifacts; started-only actions have unknown outcomes.")
        return task

    def lease_busy(self, ident):
        return lease_busy(self.directory(ident) / "worker.lock")

    def owner_alive(self, ident):
        proc = self.processes.get(ident)
        return bool(proc is not None and proc.poll() is None) or self.lease_busy(ident)

    def checkpoint(self, chat, ident, completed, next_action, evidence, blocker="", estimate_effective_tokens=None, items_done=None,
                   plan=None, estimate_turns=None, done_when="", task_type="", lesson="",
                   facts=None, guesses=None, decisions=None, completion_items=None):
        task=self.get(ident)
        if task["chat"] != chat:
            raise ValueError("Task belongs to another chat.")
        value={"completed":completed,"next_action":next_action,"evidence":evidence,
               "blocker":blocker,"updated":time.time()}
        previous = {}
        try:
            previous = json.loads((self.directory(ident)/"checkpoint.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        # Feasibility estimate and completed items persist across checkpoints unless restated.
        if estimate_effective_tokens is not None:
            value["estimate_effective_tokens"] = int(estimate_effective_tokens)
        elif previous.get("estimate_effective_tokens") is not None:
            value["estimate_effective_tokens"] = previous["estimate_effective_tokens"]
        value["items_done"] = sorted({int(i) for i in (items_done if items_done is not None else previous.get("items_done") or [])})
        # The plan (plan-first regime, 2026-10-08) persists like the estimate; a new one replaces it whole.
        # Ledger (Magentic-One task ledger, 2026-10-08): facts established, guesses still open, decisions with their owner
        # ("Alex:" or "Cato:"). Each list replaces the previous one whole when given, so the checkpoint is the current truth.
        bad = unquoted_owner_decisions(decisions)
        if bad:
            raise ValueError(DECISION_QUOTE_RULE + " Offending: " + bad)
        for key, given in (("plan", plan), ("estimate_turns", estimate_turns), ("done_when", done_when), ("task_type", task_type),
                           ("facts", facts), ("guesses", guesses), ("decisions", decisions)):
            if given:
                value[key] = given
            elif previous.get(key):
                value[key] = previous[key]
        from agent_core import verification
        # All checkpoint validation precedes the durable declaration; progress is no longer its owner.
        if completion_items is not None:
            verification.declare(self, ident, completion_items)
        atomic_json(self.directory(ident)/"checkpoint.json",value)
        if lesson.strip():
            from agent_core.learning import propose, similar
            earlier = similar(self.agent_dir, chat, lesson)           # before propose, or it would match itself
            value["candidate_lesson"] = propose(self.agent_dir, chat, lesson,
                str(self.directory(ident) / "checkpoint.json"), value.get("task_type") or "other")
            if earlier and earlier != value["candidate_lesson"]:
                value["repeat_lesson"] = {"key": earlier, "time": time.time()}
                value["lesson_note"] = REPEAT_NOTE.format(key=earlier)
            atomic_json(self.directory(ident)/"checkpoint.json", value)
        return dict(saved=True, task=ident, **{k: value[k] for k in ("candidate_lesson", "repeat_lesson", "lesson_note") if k in value})

    def repeat_lesson_open(self, ident):
        """The repeated-lesson line for the result's OPEN section when a lesson restated an earlier candidate this task;
        information for Alex, never an authorization to change a method (2026-10-10)."""
        try:
            rep = json.loads((self.directory(ident)/"checkpoint.json").read_text(encoding="utf-8")).get("repeat_lesson") or {}
        except (OSError, ValueError):
            return ""
        key = rep.get("key") if isinstance(rep, dict) else None
        return f"lesson restates candidate {key}; propose the skill edit to Alex" if key else ""

    def route_line(self, ident):
        """'[Route] provider/billing N turns; ...' from the task's usage events, so the reader sees which model did the work."""
        path = self.directory(ident) / "task_state" / "inbox.sqlite3"
        if not path.exists():
            return ""
        try:
            with closing(sqlite3.connect(path)) as db:
                rows = db.execute("SELECT provider, billing, count(*) FROM usage_events WHERE kind='response' "
                                  "GROUP BY provider, billing ORDER BY 3 DESC").fetchall()
        except sqlite3.Error:
            return ""
        return "[Route] " + "; ".join(f"{p}/{b} {n} turns" for p, b, n in rows) if rows else ""

    def lessons(self, chat, task_type="", skills=(), limit=6):
        """Open lessons as labelled hints: applied ones to check on this task, proposals awaiting Alex, candidates
        (Alex, 2026-10-10: a lesson the agent wrote yesterday should reach it today, marked as unapproved)."""
        from agent_core.learning import hints
        return hints(self.agent_dir, chat, task_type, limit)

    def unfinished_actions(self, ident):
        """Action records that started and never finished: their outcome is unknown, so a rerun could repeat them."""
        folder = self.directory(ident) / "task_state" / "actions"
        out = []
        for path in sorted(folder.glob("*.json")) if folder.exists() else []:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if record.get("status") == "started":
                out.append(record.get("tool", "?"))
        return out

    def retry_interrupted(self):
        """Requeue a worker that crashed (status interrupted by a worker error) when its own records show nothing
        started-but-unfinished, at most twice with backoff. Replaces the read_only mode: the evidence decides whether
        a rerun is safe, not a label chosen at the start."""
        with self.db() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM tasks WHERE status='interrupted'")]
        for task in rows:
            if self.owner_alive(task["id"]) or self.decisions(task["chat"], task["id"]):
                continue
            if not str(task.get("note", "")).startswith("Worker error"):
                continue                                   # cancelled, stopped by a guard, or blocked: not a crash
            path = self.directory(task["id"]) / "context.json"
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            retries = data.get("retries", 0)
            if retries >= 2 or time.time() - task["updated"] < 30 * (retries + 1):
                continue
            pending = self.unfinished_actions(task["id"])
            if pending:
                with self.db() as db:
                    db.execute("UPDATE tasks SET note=? WHERE id=? AND status='interrupted'",
                               (f"Worker error; not rerun automatically: {pending[-1]} started and never reported. Inspect, then resume.", task["id"]))
                continue
            data["retries"] = retries + 1
            atomic_json(path, data)
            with self.db() as db:
                lifecycle.move(db, task["id"], "recover", "queued",
                               "Recovery queued: worker crashed with no unfinished external action", quiet=True)

    def worker_limit(self):
        return self.role.workers

    def stop_stalled(self):
        """A worker past its own health deadline by more than STALL_GRACE seconds is hung (a model call that never
        returns, 2026-10-10): terminate its tree and mark the task interrupted; retry_interrupted resumes it."""
        with self.db() as db:
            rows = db.execute("SELECT id FROM tasks WHERE pid IS NOT NULL AND status IN ('running','needs_answer','queued')").fetchall()
        for row in rows:
            ident = row["id"]
            try:
                health = json.loads((self.directory(ident) / "task_state" / "health.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (worker_stalled(health, time.time()) and self.lease_busy(ident)
                    and health.get("pid") == self.get(ident).get("pid")):
                subprocess.run(["taskkill", "/PID", str(health["pid"]), "/T", "/F"], capture_output=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.move(ident, "stop", "interrupted", f"Worker stopped at {health.get('phase')}: no progress past its deadline. "
                          "Saved tool outcomes must be checked before repeating an action.", quiet=True)
                self.processes.pop(ident, None)

    def reap_orphans(self):
        """Reconcile durable task state with the same lease used by launch and workers."""
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            rows = list(db.execute("SELECT id,status,updated FROM tasks WHERE status IN ('starting','running','cancelling')"))
        finally:
            db.close()
        for row in rows:
            ident = row["id"]
            if row["status"] == "starting" and time.time() - row["updated"] < 30:
                continue
            if not self.owner_alive(ident):
                self.move(ident, "cancel" if row["status"] == "cancelling" else "stop",
                          "cancelled" if row["status"] == "cancelling" else "interrupted",
                          "Worker exited; inspect saved evidence before resume.", quiet=True)
                self.processes.pop(ident, None)

    def launch(self, row):
        """Spawn the worker process for a claimed row."""
        ident = row["id"]
        current = self.get(ident)
        if current.get("attempt") != row["attempt"] or current["status"] != "starting":
            return
        if (self.agent_dir / "task_state" / "drain").exists():
            self.move(ident, "defer", "queued", "Launch deferred until manager restart", quiet=True)
            return
        directory = self.directory(ident)
        env = dict(os.environ, **{self.role.env_var: str(ident)})
        try:
            with (directory / "worker.log").open("ab") as output:
                argv = [sys.executable, str(Path(__file__).resolve()), "--worker", str(ident), "--agent", str(self.agent_dir),
                        "--role", self.role.name, "--attempt", row["attempt"]]
                proc = subprocess.Popen(argv,
                                        cwd=self.agent_dir, env=env, stdout=output, stderr=subprocess.STDOUT,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.processes[ident] = proc
        except Exception as exc:
            self.move(ident, "stop", "interrupted", "Could not start worker: " + type(exc).__name__, quiet=True)

    def launch_next(self):
        """Claim the oldest queued task if a slot is free and start its worker; the task id, or None."""
        row = self.claim_next()
        if row:
            self.launch(row)
            return row["id"]
        return None

    def claim_next(self):
        """Reserve one launch identity atomically. A held lease occupies a slot regardless of task status."""
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            if (self.agent_dir / "task_state" / "drain").exists():
                return None
            rows = [dict(r) for r in db.execute("SELECT * FROM tasks ORDER BY id")]
            by_id = {r["id"]: r for r in rows}
            owners = {r["id"] for r in rows if self.owner_alive(r["id"])}
            occupied = owners | {r["id"] for r in rows if r["status"] in ("starting", "running", "cancelling")}
            queued = [r for r in rows if r["status"] == "queued" and r["id"] not in owners]
            # Two lanes: Alex's work has the worker slots; runtime work (diagnoses, trials, eval reruns) has one lane
            # of its own, so neither waits behind the other.
            real_busy = sum(1 for i in occupied if not meta(by_id[i]["request"]))
            meta_busy = len(occupied) - real_busy
            row = next((r for r in queued if not meta(r["request"])), None) if real_busy < self.worker_limit() else None
            if row is None and meta_busy < 1:
                row = next((r for r in queued if meta(r["request"])), None)
            if row is None:
                return None
            row["attempt"] = uuid.uuid4().hex
            lifecycle.move(db, row["id"], "claim", "starting", notified=None, attempt=row["attempt"], pid=None)
            db.commit()
            return row
        finally:
            db.close()

    def report_worker_failures(self, send):
        """Crash delivery is independent of result ownership and survives automatic retries/restarts.

        One notice per incident (until explicit resume), acknowledged only after sending succeeds.
        """
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            rows = list(db.execute("SELECT f.task,f.note,t.chat,t.request,t.status FROM worker_failures f "
                                   "JOIN tasks t ON t.id=f.task WHERE f.notified=0"))
            for row in rows:
                task = dict(row)
                delivered = send(task["chat"], f"{self.role.name} background task {task['task']} ({self.label(task)}) stopped. "
                     + task["note"] + " Progress is saved. Safe automatic retries are limited to two; check task status before resuming.")
                if delivered is False:
                    raise OSError("Worker failure notification was not delivered")
                db.execute("UPDATE worker_failures SET notified=1 WHERE task=?", (task["task"],))
                db.commit()
        finally:
            db.close()

    def tick(self, send):
        self.retry_interrupted()
        if time.time()-self.last_monitor>60:
            self.last_monitor=time.time()
            self.projects.monitor(send,self)
        if time.time() - self.last_delegate_poll > DELEGATE_POLL_SECONDS and not (
                self.delegate_poll_thread and self.delegate_poll_thread.is_alive()):
            self.last_delegate_poll = time.time()
            # Off the 2-second monitor loop: a status subprocess can take tens of seconds.
            self.delegate_poll_thread = threading.Thread(target=self.poll_waiting, daemon=True, name="delegate-poll")
            self.delegate_poll_thread.start()
        for ident, proc in list(self.processes.items()):
            if proc.poll() is not None and self.get(ident)["status"] not in ACTIVE:
                self.processes.pop(ident, None)
        with self.db() as db:
            active = [dict(r) for r in db.execute("SELECT * FROM tasks WHERE status IN ('starting','running','cancelling')")]
        for task in active:
            ident = task["id"]
            proc = self.processes.get(ident)
            if task["status"] == "cancelling" and proc and proc.poll() is None:
                # Only terminate the process object launched by this manager, including reader children.
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            dead = (proc is not None and proc.poll() is not None and not self.lease_busy(ident))
            if proc is None and time.time() - task["updated"] > 30:
                dead = not self.lease_busy(ident)
            if dead:
                self.move(ident, "cancel" if task["status"] == "cancelling" else "stop",
                          "cancelled" if task["status"] == "cancelling" else "interrupted",
                          "Worker stopped. Saved tool outcomes must be checked before repeating an action.", quiet=True)
                self.processes.pop(ident, None)
        self.stop_stalled()
        self.reap_orphans()
        self.report_worker_failures(send)
        self.launch_next()
        with self.db() as db:
            notices = [dict(r) for r in db.execute("SELECT * FROM tasks WHERE status NOT IN ('queued','starting','running','cancelling') AND notified=0")]
        for task in notices:
            if self.lease_busy(task["id"]):
                continue  # A decision may be durable before its worker has saved the final result.
            if task["status"] == "interrupted":
                continue  # failure ledger owns crash delivery, including delegated work
            # Delivery ownership is checked here for every state producer, including atomic decision transitions.
            db = sqlite3.connect(self.database, timeout=30)
            try:
                table = self.role.request_table
                mapped = table and db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone()
                if mapped and db.execute(f"SELECT 1 FROM {table} WHERE task=?", (task["id"],)).fetchone():
                    db.execute("UPDATE tasks SET notified=1 WHERE id=?", (task["id"],))
                    db.commit()
                    continue
            finally:
                db.close()
            context_file = self.directory(task["id"]) / "context.json"
            context = json.loads(context_file.read_text(encoding="utf-8")) if context_file.exists() else {}
            if context.get("notify_telegram") is False:          # delegate requests report to the caller, not the chat
                with self.db() as db:
                    db.execute("UPDATE tasks SET notified=1 WHERE id=?", (task["id"],))
                continue
            result = self.directory(task["id"]) / "result.txt"
            text = result.read_text(encoding="utf-8") if result.exists() and task["status"] in ("finished", "needs_attention", "needs_answer", "capped", "infeasible") else task["note"]
            # Internal task titles/statuses belong in management views, not conversation.
            if task['status'] == 'cancelled':
                text = "I've stopped the background work. Anything already saved is still available."
            elif task['status'] not in ('finished', 'needs_attention', 'needs_answer', 'capped'):
                text = "The background work stopped before I could finish. I've kept the request and saved progress, so it can be picked up from there."
            if task["status"] == "needs_answer":
                # The question goes out with its decision id, so the message that asks it is recorded on the decision
                # and Alex's reply to that message answers it directly (2026-10-21).
                asked = [d for d in self.decisions(task["chat"], task["id"]) if d.get("kind", "question") == "question"]
                if asked:
                    from agent_core.reply_envelope import Reply
                    text = Reply(text)
                    text.decision_id = asked[0]["id"]
            quoted = context.get("message_id")                 # the result quotes its request when Alex has moved on
            delivered = send(task["chat"], text, quoted) if quoted else send(task["chat"], text)
            if delivered is False:
                raise OSError("Task result notification was not delivered")
            with self.db() as db:
                db.execute("UPDATE tasks SET notified=1 WHERE id=?", (task["id"],))

    def monitor(self, send):
        self.monitoring = True          # start() may launch directly once the queue is being served
        def loop():
            while True:
                try:
                    self.tick(send)
                except Exception as exc:
                    print("[background] monitor:", type(exc).__name__)
                time.sleep(2)
        threading.Thread(target=loop, daemon=True, name="background-monitor").start()

DECISION_QUOTE_RULE = ("A decision labelled 'Alex:' must contain his words in quotation marks; "
                       "otherwise label it 'Cato:' (your assumption) or 'System:' (a runtime rule).")
QUOTE_MARKS = ('"', '\u201c', '\u201d', '\u300c', '\u300d', '\u00ab', '\u00bb')

def unquoted_owner_decisions(decisions):
    """Comma-joined 'Alex:' decisions that carry no quoted words; '' when all are fine."""
    items = decisions if isinstance(decisions, (list, tuple)) else ([decisions] if decisions else [])
    bad = [str(d).strip() for d in items
           if re.match(r"\s*alex\s*:", str(d), re.I) and not any(q in str(d) for q in QUOTE_MARKS)]
    return "; ".join(b[:80] for b in bad)

def unquoted_owner_lines(text):
    """Same check over the lines of a brief."""
    return unquoted_owner_decisions([re.sub(r"^\s*-\s*", "", l) for l in (text or "").splitlines() if re.match(r"\s*(-\s*)?alex\s*:", l, re.I)])



def skill_output_contracts(store, ident, agent):
    """[(skill name, its '## Output' section)] for every skill this task loaded, in load order."""
    names = []
    folder = store.directory(ident) / "task_state" / "actions"
    for path in sorted(folder.glob("*.json")) if folder.exists() else []:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if record.get("tool") == "skill" and record.get("status") == "finished":
            name = (record.get("inputs") or {}).get("name")
            if name and name not in names:
                names.append(name)
    if not names:
        return []
    try:
        skills = agent.discover_skills()
    except Exception:
        return []
    out = []
    for name in names:
        body = (skills.get(name) or {}).get("body", "")
        head = re.search(r"^## Output\b.*$", body, re.M)
        if not head:
            continue
        section = body[head.start():]
        nxt = re.search(r"^## ", section[head.end() - head.start():], re.M)
        if nxt:
            section = section[:head.end() - head.start() + nxt.start()]
        out.append((name, section.strip()))
    return out


def worker(ident, agent_dir, role, attempt=None):
    sys.path.insert(0, str(Path(agent_dir).resolve()))
    store = TaskStore(agent_dir, role)
    folder = store.directory(ident)
    lease = WorkerLease(folder / "worker.lock")
    if not lease.acquire():
        return  # Another worker owns the task; never overwrite its state or its PID.
    try:
        task = store.get(ident)
        if attempt is None:
            # A pre-upgrade manager may have spawned us while draining. Adopt only its unclaimed row,
            # under the OS lease, and assign the same identity every new launch requires.
            if task.get("attempt") is not None or task["status"] != "starting":
                return
            attempt = uuid.uuid4().hex
            db = sqlite3.connect(store.database, timeout=30)
            try:
                changed = db.execute("UPDATE tasks SET attempt=? WHERE id=? AND attempt IS NULL AND status='starting'",
                                     (attempt, ident)).rowcount
                db.commit()
            finally:
                db.close()
            if not changed:
                return
        elif task.get("attempt") != attempt:
            return
        store.worker_attempt = attempt
        if task["status"] == "cancelling":
            store.move(ident, "cancel", "cancelled", "Cancelled before model execution.")
            return
        with store.db() as db:
            from agent_core import release
            claimed = lifecycle.move(db, ident, "run", "running", "Worker started", notified=None, quiet=True,
                                     attempt_is=attempt, pid=os.getpid(), release=release.commit())
        if not claimed:
            return
        try:
            import importlib.util
            from agent_core import trials
            task_context = json.loads((folder / "context.json").read_text(encoding="utf-8")) if (folder / "context.json").exists() else {}
            trials.prepare_worker(task_context, folder)          # a trial arm reads the edited skill, before the agent loads
            spec = importlib.util.spec_from_file_location(f"{store.role.name}_worker", store.agent_dir / "telegram.py")
            agent = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(agent)
            chat = task["chat"]
            agent.load_model_prefs(chat)
            agent.runtime._background = store
            if trials.isolated(task_context):
                trials.scope(agent.runtime, task_context)
            if (not store.role.owner_budget and not agent._routing.enabled(agent.AGENT_DIR) and agent.get_provider(chat) == "openai"
                    and not agent._chatgpt_plan.store.exists() and os.environ.get(f"{store.role.name.upper()}_ALLOW_API", "0") != "1"):
                store.move(ident, "stop", "interrupted", f"Blocked: {store.role.name.title()} needs his own ChatGPT sign-in before model execution.")
                return
            agent._inbox.active_job = ident
            agent._inbox.active_chat = chat
            agent._inbox.local.job = ident
            saved = agent._conversation_path(chat).exists()
            messages = agent.load_conversation(chat)
            if not saved:
                context = json.loads((folder / "context.json").read_text(encoding="utf-8"))
                if (context.get("eval") or {}).get("benchmark"):
                    context["context"] += f"\nEvaluation output directory: {folder / 'trial_output'}. Write the requested deliverables there."
                from agent_core.learning import task_type_for
                # A trial arm gets no lesson hints: the proposed edit is the only difference between its two runs.
                lessons = "" if context.get("trial") else store.lessons(chat, task_type_for(task["request"]))
                messages.append({"role": "user", "content": task["request"] + "\n\nTask context:\n" + context["context"]
                                 + ("\n\nLessons from earlier tasks, each labelled with its status:\n" + lessons if lessons else "")})
            else:
                from agent_core.learning import task_type_for
                lessons = store.lessons(chat, task_type_for(task["request"]))
                messages.append({"role": "user", "content": "Continue from the current task contract and its proof status. Inspect actions with missing outcomes before repeating them; never automatically resend emails." + ("\n\nLessons from earlier tasks, each labelled with its status:\n" + lessons if lessons else "")})
            agent.conversations[chat] = messages
            agent.save_conversation(chat)
            agent.runtime.SYSTEM_PROMPT = agent.SYSTEM_PROMPT + (
                "\nYour final response goes directly to the user as a normal chat message. "
                "Use your own established persona and the user's conversational language. "
                "Start with the actual outcome and name the subject naturally when needed. "
                "Do not recite the internal task prompt, task number, status label or workflow. "
                "If blocked, say what you could not do, the practical reason, and the next useful step. "
                "Translate internal errors into plain language; quote diagnostics only when the user asks "
                "or the exact detail is needed to act. Never say finished or done unless the requested "
                "work actually succeeded. Do not promise recovery unless it has actually been started. "
                "Keep relevant findings, evidence and deliverables; avoid an administrative preamble."
            )
            from agent_core.memory_store import atomic_text
            try:
                result = agent.run_turn(messages, chat)
            except agent.RunawayStop as exc:
                # Runtime's own words only. The model's last text is not a reply (it can be working notes), and only
                # the declared reply field or a runtime status ever reaches Alex (2026-10-12).
                atomic_text(folder / "result.txt", "I stopped this background task: it kept repeating tool calls that "
                            "failed or were refused, so continuing would not have changed the result. Everything it "
                            "completed is saved. Detail: " + str(exc))
                store.move(ident, "stop", "needs_attention", "Stopped: repeated failed or refused tool calls; see the result.")
                return
            agent.save_conversation(chat)
            # No budget branch here since 2026-10-08: nothing pauses or caps a worker. Loops end in RunawayStop above;
            # cost is reported in the result and the awareness line.
            result = trials.after_arm(agent, store, ident, task_context, result)
            atomic_text(folder / "result.txt", result or "No final answer generated; inspect saved tool records.")
            outcome = result
            repeat = store.repeat_lesson_open(ident)
            if repeat:
                result = (result or "") + "\nOPEN: " + repeat
            unrun = store.unrun_line(ident)                 # what Alex is about to touch that nobody here ran
            if unrun:
                result = (result or "") + "\n" + unrun
            account = store.delegate_account(ident)         # the delegate's own words under the verdict (2026-10-24)
            if account:
                result = (result or "") + "\n\n" + account
            route = store.route_line(ident)
            if route:
                result = (result or "") + "\n" + route
            atomic_text(folder / "result.txt", result)
            state = store.finish(ident, outcome)
            if state == WAITING:
                return
            if state == "needs_attention":
                decision = json.loads((folder / "completion.json").read_text(encoding="utf-8"))
                atomic_text(folder / "result.txt", "Completion not verified: " + "; ".join(decision["reasons"]) + "\n\n" + str(result))
        except TaskStopped:
            store.move(ident, "cancel", "cancelled", "Stopped; completed actions remain recorded.", quiet=True)
        except Exception as exc:
            store.move(ident, "stop", "interrupted", "Worker error: " + type(exc).__name__ + ". Inspect worker.log and saved actions before resuming.", quiet=True)
            import traceback
            traceback.print_exc()
    finally:
        lease.close()

if __name__ == "__main__":
    # A worker process: python background_runtime.py --worker <id> --agent <agent dir> (TaskStore.launch).
    # The agent's code imports this module by name; alias it so the process holds one copy, not two.
    sys.modules.setdefault("background_runtime", sys.modules[__name__])
    args = sys.argv[1:]
    if len(args) < 4 or args[0] != "--worker" or "--agent" not in args:
        sys.exit("usage: background_runtime.py --worker <task id> --agent <agent dir> [--role <name>]")
    role = ROLES[args[args.index("--role") + 1]] if "--role" in args else ROLES["cato"]
    worker(int(args[1]), Path(args[args.index("--agent") + 1]), role,
           args[args.index("--attempt") + 1] if "--attempt" in args else None)
