"""Durable decisions and evidence-backed task reporting, shared by every agent."""
import json
import sqlite3
import time
from pathlib import Path
from datetime import datetime
from agent_core.reply_envelope import Reply
from agent_core.outcome import Failed
from agent_core.worker_lease import busy
from agent_core import lifecycle

TOOL = {
    "name": "task_decision",
    "description": "Persist a task's question BEFORE asking it; list unanswered decisions; answer using an exact user quote. When Alex's current message is the answer, give decision_id and the quote and leave source_id out; otherwise give source_id from search_conversations. When he uses Telegram's reply on the question, the runtime has already recorded it. Answer queues continuation atomically. Never substitute an assistant suggestion for the user's decision. Parked actions (kind=action) are approved only by his own yes, never through this tool.",
    "input_schema": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["list", "ask", "answer"]},
        "task_id": {"type": "integer"}, "question": {"type": "string"},
        "decision_id": {"type": "integer"}, "answer": {"type": "string"},
        "source_id": {"type": "integer"}, "quote": {"type": "string"}},
        "required": ["action"]},
}


def pending(store, chat, task=None, kind=None):
    db = sqlite3.connect(store.database, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        query = "SELECT * FROM decisions WHERE chat=? AND state='pending'"
        args = [chat]
        if task is not None:
            query += " AND task=?"
            args.append(int(task))
        if kind is not None:
            query += " AND kind=?"
            args.append(kind)
        rows = [dict(r) for r in db.execute(query + " ORDER BY id", args)]
    finally:
        db.close()
    return annotate_stale(store, chat, rows)


STOP_TERMS = {"的", "了", "是", "我", "你", "他", "们", "这", "那", "不", "都", "也", "在", "有", "和", "就", "要", "可以",
              "什么", "一个", "选项", "推荐", "还是", "如果", "the", "and", "for", "with", "that", "this", "option"}


def terms(text):
    """Latin words of three letters or more and CJK character bigrams, minus function words: the overlap test that
    says a later message or task may be about a pending question."""
    import re
    out = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", text)}
    cjk = re.findall(r"[一-鿿]+", text)
    for run in cjk:
        out.update(run[i:i + 2] for i in range(len(run) - 1))
    return {t for t in out if t not in STOP_TERMS and not any(c in t for c in "的了是我你他们这那不都也在有和就要")}


def _stamp(said_at):
    try:
        return datetime.fromisoformat(said_at).timestamp()
    except (TypeError, ValueError):
        return 0


def annotate_stale(store, chat, rows):
    """Mark each pending decision 'possibly_stale' with a reason when, since it was asked: Alex sent a message that
    overlaps its topic, its task finished or was cancelled or superseded, or a newer task on the same topic was
    started. Nothing is closed here: the flag only stops a stale question from looking current (2026-10-20: decision 7
    was answered at 18:24 through the inbox, never closed, and asked again at 19:56)."""
    if not rows:
        return rows
    recall = store.agent_dir / "task_state/conversation_recall.sqlite3"
    messages, tasks = [], []
    since = min(r["created"] for r in rows)
    if recall.exists():
        try:
            db = sqlite3.connect(recall.as_uri() + "?mode=ro", uri=True)
            try:
                messages = [m for m in db.execute("SELECT id,text,said_at FROM messages WHERE chat=? AND speaker='user' "
                                                  "AND source NOT LIKE '%task_state' || ? || 'background%' ORDER BY id",
                                                  (str(chat), "%"))
                            if _stamp(m[2]) > since - 60]
            finally:
                db.close()
        except sqlite3.Error:
            messages = []
    try:
        db = sqlite3.connect(store.database, timeout=30)
        try:
            tasks = db.execute("SELECT id,status,request,created FROM tasks WHERE chat=? AND created>? ORDER BY id",
                               (chat, since - 60)).fetchall()
            states = dict(db.execute("SELECT id,status FROM tasks WHERE id IN (%s)" % ",".join("?" * len(rows)),
                                     [r["task"] for r in rows]).fetchall())
        finally:
            db.close()
    except sqlite3.Error:
        states = {}
    for row in rows:
        topic = terms(row["question"])
        reason = ""
        status = states.get(row["task"])
        if status in ("finished", "cancelled", "superseded"):
            reason = f"task {row['task']} is {status}"
        for ident, text, said_at in messages:
            if _stamp(said_at) <= row["created"] - 60:
                continue
            shared = sorted(topic & terms(text), key=len, reverse=True)
            if shared:
                when = said_at[11:16] if said_at and len(said_at) >= 16 else said_at
                reason = (reason + "; " if reason else "") + f"user message {ident} at {when} mentions {shared[0]}"
                break
        for ident, status, request, created in tasks:
            if ident != row["task"] and created > row["created"] and topic & terms(request or ""):
                reason = (reason + "; " if reason else "") + f"newer task {ident} is about the same topic"
                break
        if reason:
            row["possibly_stale"] = reason
    return rows


def ask(store, chat, ident, question):
    question = question.strip()
    if not question or len(question) > 6000:
        raise ValueError("A decision needs a nonempty question, at most 6000 characters.")
    db = sqlite3.connect(store.database, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN IMMEDIATE")
        task = db.execute("SELECT * FROM tasks WHERE id=? AND chat=?", (ident, chat)).fetchone()
        if ident and not task:
            raise ValueError("Task is missing.")
        if ident and task["status"] == "finished":
            raise ValueError(f"Task {ident} is finished. A question about it is a conversation question (task_id 0); "
                             "if it needs more work, reopen it first with background_task resume.")
        if store.worker_attempt and (not task or task["attempt"] != store.worker_attempt):
            raise ValueError("Worker no longer owns this task.")
        old = db.execute("SELECT * FROM decisions WHERE task=? AND chat=? AND state='pending' AND kind='question'",
                         (ident, chat)).fetchone()
        if old:
            # Never silently replace a question already shown to the user.
            if old["question"] != question:
                raise ValueError("This task already has a pending question; resolve it before asking another.")
            return dict(old)
        now = time.time()
        key = db.execute("INSERT INTO decisions(task,chat,question,created,updated) VALUES (?,?,?,?,?)",
                         (ident, chat, question, now, now)).lastrowid
        if ident:
            # notified=1: the question is delivered by whoever asked it (the foreground reply that carries it, or
            # the worker's finish move, which resets the flag for the monitor to send result.txt). Leaving the
            # default 0 made the monitor re-send the task's OLD result each time Cato asked about it from the
            # foreground: "Portable Tiro is ready to use" went out three times on 2026-10-22 (19:41, 21:03, 21:04).
            lifecycle.move(db, ident, "ask", "needs_answer", "Waiting for decision #" + str(key), notified=1)
        db.commit()
        return dict(db.execute("SELECT * FROM decisions WHERE id=?", (key,)).fetchone())
    finally:
        db.close()


YES = {"yes", "y", "ok", "okay", "go", "好", "好的", "可以", "行", "同意", "确认", "执行", "是"}
NO = {"no", "n", "cancel", "stop", "不", "不要", "不行", "取消", "别", "算了"}


def verdict(text):
    """True for a plain yes, False for a plain no, None for anything else. Punctuation and case do not matter."""
    word = "".join(ch for ch in (text or "").strip().lower() if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
    return True if word in YES else False if word in NO else None


def park_action(store, chat, tool, inputs, question):
    """A guarded tool call waits as a decision with its exact inputs (2026-10-21). It has no deadline and survives a
    restart: Alex's yes, whenever it comes, runs exactly this call once. Replaces the 60-second in-memory wait,
    which treated a late answer as a no and forgot everything on restart."""
    now = time.time()
    db = sqlite3.connect(store.database, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        key = db.execute("INSERT INTO decisions(task,chat,question,created,updated,kind,action) VALUES (0,?,?,?,?,'action',?)",
                         (chat, question, now, now, json.dumps({"tool": tool, "inputs": inputs}, ensure_ascii=False))).lastrowid
        db.commit()
        return dict(db.execute("SELECT * FROM decisions WHERE id=?", (key,)).fetchone())
    finally:
        db.close()


def bind_messages(store, decision, message_ids):
    """Record the Telegram messages that asked decision `decision`; a reply to any of them answers it."""
    db = sqlite3.connect(store.database, timeout=30)
    try:
        row = db.execute("SELECT message_ids FROM decisions WHERE id=?", (int(decision),)).fetchone()
        if row is None:
            return
        ids = json.loads(row[0] or "[]") + [int(m) for m in message_ids]
        db.execute("UPDATE decisions SET message_ids=? WHERE id=?", (json.dumps(ids), int(decision)))
        db.commit()
    finally:
        db.close()


def by_message(store, chat, message_id):
    """The pending decision that Telegram message `message_id` asked, or None."""
    for row in pending(store, chat):
        if int(message_id) in json.loads(row.get("message_ids") or "[]"):
            return row
    return None


def settle_action(store, chat, decision, approved, source, outcome=""):
    """Close a parked action: 'answered' when Alex said yes (and it ran), 'declined' when he said no. Returns False
    if it was no longer pending, so a repeated yes never runs the action twice."""
    db = sqlite3.connect(store.database, timeout=30)
    try:
        changed = db.execute("UPDATE decisions SET state=?,answer=?,source=?,updated=? WHERE id=? AND chat=? "
                             "AND state='pending' AND kind='action'",
                             ("answered" if approved else "declined", outcome[:4000], source, time.time(),
                              int(decision), chat)).rowcount
        db.commit()
        return bool(changed)
    finally:
        db.close()


def answer_from_current(agent, chat, decision, answer, quote):
    """Answer a decision from the message Alex sent in this turn: no archive search, the quote is checked against
    that message itself."""
    inbox = agent._inbox
    message_id = inbox.answering() if hasattr(inbox, "answering") else None
    if not message_id:
        raise ValueError("No current message from Alex in this turn; give source_id from search_conversations.")
    with inbox.db() as db:
        row = db.execute("SELECT payload FROM jobs WHERE chat=? AND message=?", (chat, message_id)).fetchone()
    text = ""
    if row:
        raw = json.loads(row[0])
        text = raw.get("text") or raw.get("caption") or ""
    if not quote.strip() or quote not in text:
        raise ValueError("The quote is not in Alex's current message.")
    return answer_record(agent._background, chat, decision, answer,
                         json.dumps({"message_id": message_id, "quote": quote}, ensure_ascii=False))


def genuine_source(store, chat, source):
    """Alex's own words reach the archive two ways: the chat's conversation file, and the inbox queue for a message
    that arrived while the agent was busy (2026-10-20: his 18:24 answer sat in inbox.sqlite3 and was refused as a
    quote). Both are his. A worker's copy under task_state/background is a brief, not his message."""
    base = source.split("#", 1)[0]
    try:
        path = Path(base).resolve()
    except OSError:
        return False
    if path == (store.agent_dir / f"conversation_{chat}.json").resolve():
        return True
    return path == (store.agent_dir / "task_state" / "inbox.sqlite3").resolve() and "#job=" in source


def answer_user(store, chat, decision, answer, source_id, quote):
    path = store.agent_dir / "task_state/conversation_recall.sqlite3"
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        row = db.execute("SELECT speaker,text,source,said_at FROM messages WHERE id=? AND chat=?",
                         (int(source_id), str(chat))).fetchone()
    finally:
        db.close()
    if not quote.strip() or not row or row[0] != "user" or quote not in row[1]:
        raise ValueError("Exact user quote not found in this chat.")
    if not genuine_source(store, chat, row[2]):
        raise ValueError("The quote must come from the user's conversation, not a worker brief or synthetic context.")
    decision_row = next((d for d in store.decisions(chat) if d["id"] == decision), None)
    if decision_row:
        if not row[3] or datetime.fromisoformat(row[3]).timestamp() + 60 < decision_row["created"]:
            raise ValueError("The quoted answer predates this decision.")
    return answer_record(store, chat, decision, answer, json.dumps(
        {"source_id": source_id, "quote": quote}, ensure_ascii=False))


def answer_record(store, chat, decision, answer, source):
    """One commit: resolution + durable control + runnable state. Caller verifies authority."""
    if not answer.strip():
        raise ValueError("Supply the answer.")
    db = sqlite3.connect(store.database, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM decisions WHERE id=? AND chat=?", (decision, chat)).fetchone()
        if not row:
            raise ValueError("Unknown decision in this chat.")
        if row["state"] in ("answered", "delivered") and row["answer"] == answer and row["source"] == source:
            return dict(row)
        if row["state"] != "pending":
            raise ValueError("Decision is no longer pending.")
        if row["kind"] == "action":
            raise ValueError("A parked action runs only on Alex's own yes to it, not through task_decision.")
        task = db.execute("SELECT status FROM tasks WHERE id=?", (row["task"],)).fetchone()
        if row["task"] == 0:
            db.execute("UPDATE decisions SET state='answered',answer=?,source=?,updated=? WHERE id=?",
                       (answer, source, time.time(), decision))
            db.commit()
            return dict(db.execute("SELECT * FROM decisions WHERE id=?", (decision,)).fetchone())
        if task[0] in ("cancelled", "cancelling", "superseded"):
            raise ValueError("Task was cancelled or superseded.")
        control = db.execute("INSERT INTO controls(task,text) VALUES (?,?)",
            (row["task"], f"Decision #{decision} answered.\nQuestion: {row['question']}\nAnswer: {answer}\n"
             f"Source: {source}\nContinue from saved evidence; clear the resolved checkpoint blocker.")).lastrowid
        now = time.time()
        db.execute("UPDATE decisions SET state='answered',answer=?,source=?,control=?,updated=? WHERE id=?",
                   (answer, source, control, now, decision))
        # An owner that is still exiting keeps its lease. launch_next cannot start a duplicate.
        if task[0] == lifecycle.WAITING:
            lifecycle.move(db, row["task"], "answer", lifecycle.WAITING, "Decision answered; still waiting on the delegate")
        else:
            lifecycle.move(db, row["task"], "answer", "queued", "Decision answered; continuation queued")
        db.execute("DELETE FROM worker_failures WHERE task=?", (row["task"],))
        db.commit()
        return dict(db.execute("SELECT * FROM decisions WHERE id=?", (decision,)).fetchone())
    finally:
        db.close()


def completion_evidence(store, ident, result, context, checkpoint):
    """One evidence decision. A record-only run ending is not a claim that its work passed."""
    from agent_core import verification, regression
    from agent_core.corrections import is_diagnosis
    task = store.get(ident)
    reasons, items = [], []
    record_only = bool(context.get("trial") or is_diagnosis(task["request"]))
    if record_only:
        return dict(passed=False, verdict="recorded", reasons=["Record saved; not verified task completion."], items=[])
    if getattr(result, "task_outcome", "none") != "completed" or not getattr(result, "complete", False):
        reasons.append("The worker did not declare a complete result.")
    if getattr(result, "question", "") or checkpoint.get("blocker"):
        reasons.append("An unresolved question or checkpoint blocker remains.")
    benchmark = (context.get("eval") or {}).get("benchmark")
    if benchmark:
        try:
            case = regression.bound_case(store.agent_dir, context)
        except ValueError as exc:
            reasons.append(str(exc))
        else:
            grade = regression.grade(None, case, str(result), store.directory(ident) / "trial_output")
            items = grade["items"]
            if not grade["passed"]:
                reasons.append(grade["note"])
    else:
        requirements, _ = verification.contract(store, ident)
        missing = verification.uncovered_sources(store, ident, requirements)
        if missing:
            reasons.append("Instructions without declared requirements: " + str(missing))
        try:
            verification.validate_items(requirements)
            items = [dict(id=n, item=it["item"], status=verification.status(store, ident, "", n))
                     for n, it in enumerate(requirements, 1)]
            for it in items:
                if it["status"] != "passed":
                    reasons.append(f"Requirement {it['id']} {it['item']}: {it['status']}.")
        except (ValueError, TypeError, KeyError) as exc:
            reasons.append("Completion requirements unavailable: " + str(exc))
    # An accepted dependency must still match its proof when the parent finishes.
    for delegation in store.delegations(ident):
        if delegation["status"] != "read":
            continue
        request = delegation["request_id"]
        try:
            requirements, _ = verification.contract(store, ident, request)
            verification.validate_items(requirements)
            if any(verification.status(store, ident, request, n) != "passed" for n in range(1, len(requirements) + 1)):
                reasons.append(f"Dependency {request}: proof is failed or unverified.")
        except (ValueError, TypeError, KeyError) as exc:
            reasons.append(f"Dependency {request}: {exc}")
    return dict(passed=not reasons, verdict="passed" if not reasons else "unverified", reasons=reasons, items=items)


def finish(store, ident, result):
    """The only completion decision: durable controls, dependencies and current artifact proof."""
    from agent_runtime import atomic_json
    from agent_core.memory_store import atomic_text
    from agent_core.corrections import is_diagnosis
    folder = store.directory(ident)
    errors = []
    values = []
    for name in ("context.json", "checkpoint.json"):
        path = folder / name
        try:
            value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            if not isinstance(value, dict):
                raise ValueError("Expected an object")
        except (OSError, ValueError) as exc:
            value = {}
            errors.append(f"Unreadable {name}: {type(exc).__name__}")
        values.append(value)
    context, checkpoint = values
    task = store.get(ident)
    isolated = bool(context.get("trial") or context.get("eval") or is_diagnosis(task["request"]))
    question = getattr(result, "question", "") or store.legacy_question(ident)
    if question and not isolated and not store.decisions(task["chat"], ident) and task["status"] not in ("cancelled", "cancelling", "queued"):
        store.ask(task["chat"], ident, question)
    # One inexpensive dependency refresh, never another model turn or a polling loop.
    if store.unread_delegations(ident):
        store.refresh_delegations(ident)
    from agent_core import verification
    try:
        verification.saved_items(store, ident)
        for dependency in store.delegations(ident):
            verification.saved_items(store, ident, dependency["request_id"])
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        errors.append("Requirement contract unavailable: " + str(exc))
    db = sqlite3.connect(store.database, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN IMMEDIATE")
        task = db.execute("SELECT * FROM tasks WHERE id=?", (ident,)).fetchone()
        if store.worker_attempt and task["attempt"] != store.worker_attempt:
            raise ValueError("Worker no longer owns this task.")
        if task["status"] in ("cancelled", "superseded"):
            return task["status"]
        if task["status"] == "cancelling":
            lifecycle.move(db, ident, "cancel", "cancelled", "Worker returned after cancellation")
            db.commit()
            return "cancelled"
        evidence = (dict(passed=False, verdict="unverified", reasons=[], items=[]) if errors
                    else completion_evidence(store, ident, result, context, checkpoint))
        evidence["reasons"].extend(errors)
        pending = list(db.execute("SELECT request_id,status FROM delegations WHERE task=? AND status NOT IN ('read','cancelled')", (ident,)))
        decisions = db.execute("SELECT 1 FROM decisions WHERE task=? AND state='pending'", (ident,)).fetchone()
        controls = db.execute("SELECT 1 FROM controls WHERE task=? AND applied=0", (ident,)).fetchone()
        state = "finished" if not errors and (evidence["passed"] or evidence["verdict"] == "recorded") else "needs_attention"
        if decisions:
            state = "needs_attention" if isolated else "needs_answer"
            evidence["reasons"].append("An unanswered decision remains.")
        elif controls:
            state = "queued" if task["status"] == "queued" else "needs_attention"
            evidence["reasons"].append("New instructions have not been delivered to the worker.")
        elif pending:
            state = lifecycle.WAITING if any(d["status"] == "submitted" for d in pending) else "needs_attention"
            evidence["reasons"].append("Unaccepted dependencies: " + ", ".join(d["request_id"] for d in pending))
        evidence.update(state=state, checked_at=time.time())
        if state != "finished" or errors:
            evidence.update(passed=False, verdict="unverified")
        atomic_json(folder / "completion.json", evidence)
        if state == lifecycle.WAITING:
            atomic_text(folder / "parked_answer.txt", str(result or ""))
            context["parked_at"] = time.time()
            atomic_json(folder / "context.json", context)
        operation = "park" if state == lifecycle.WAITING else "finish"
        lifecycle.move(db, ident, operation, state, "Completion: " + state + "; " + "; ".join(evidence["reasons"]),
                       notified=1 if state == lifecycle.WAITING else 0)
        db.commit()
        return state
    finally:
        db.close()


def live(store, chat, ident):
    task = store.get(ident)
    if task["chat"] != chat:
        raise ValueError("Task belongs to another chat.")
    folder = store.directory(ident)
    held = busy(folder / "worker.lock")
    try:
        health = json.loads((folder / "task_state/health.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        health = {}
    # Lease alone says "owned", not "making progress". A bounded, current heartbeat is required.
    deadline = health.get("deadline") or 0
    healthy = held and health.get("pid") == task.get("pid") and float(deadline) >= time.time()
    state = task["status"]
    decision_pending = bool(store.decisions(chat, ident))
    if decision_pending:
        actual = ('running' if healthy else 'stopped') if held else 'waiting'
    elif state == "running" and healthy:
        actual = "running"
    elif state in ("queued", "starting"):
        actual = "queued"
    elif state in ("needs_answer", "waiting_on_delegate"):
        actual = "waiting"
    elif state == "finished":
        actual = "finished"
    else:
        actual = "stopped"
    return {"task_id": ident, "label": store.label(task), "state": actual, "recorded_state": state, "worker_owned": held,
            "heartbeat_current": bool(healthy), "pause_pending": decision_pending and held, "checked_at": time.time()}


def context(store, chat, task=None):
    db = sqlite3.connect(store.database, timeout=30)
    try:
        ids = [r[0] for r in db.execute(
            "SELECT id FROM tasks WHERE chat=? AND status NOT IN ('finished','cancelled','superseded') "
            "AND (? IS NULL OR id=?) ORDER BY id", (chat, task, task))]
    finally:
        db.close()
    decisions = store.decisions(chat, task)
    if not ids and not decisions:
        return ""
    return "[Live task evidence; historical assistant claims are not current status]\n" + json.dumps(
        {"tasks": [store.live(chat, i) for i in ids], "pending_decisions": decisions}, ensure_ascii=False)


def execute(agent, chat, inputs):
    try:
        action = inputs["action"]
        store = agent._background
        if action == "list":
            result = store.decisions(chat)
        elif action == "ask":
            ident = int(inputs.get("task_id") or agent.BACKGROUND_TASK_ID or 0)
            if agent.BACKGROUND_TASK_ID and ident != int(agent.BACKGROUND_TASK_ID):
                raise ValueError("A worker can only ask about its own task.")
            result = store.ask(chat, ident, inputs["question"])
        elif action == "answer":
            if agent.BACKGROUND_TASK_ID:
                raise ValueError("The foreground agent records user answers.")
            if inputs.get("source_id"):
                result = store.answer(chat, inputs["decision_id"], inputs["answer"], inputs["source_id"], inputs["quote"])
            else:
                result = answer_from_current(agent, chat, inputs["decision_id"], inputs["answer"], inputs["quote"])
        else:
            raise ValueError("Use list, ask or answer.")
        return json.dumps(result, ensure_ascii=False)
    except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
        return Failed("Decision not changed: " + str(exc))


def _log_mismatch(agent, mismatches):
    """A reply whose task statuses disagree with the records is logged, not annotated. Until 2026-10-20 22:09 a
    line like "（状态更正：任务 241 在等您决定）" was appended to Alex's chat: internal task numbers, from records that
    were themselves stale (decision 10 was old and Cato was right), stuck even onto Cato's apology for them. The
    records are attached to every turn before the agent writes; after it writes, a disagreement is evidence for
    the logs and the trace, never text in his chat."""
    label = "unchecked" if mismatches is None else ";".join(f"{r.get('task_id')}:{r.get('state')}" for r in mismatches)
    print(f"[status audit] reply disagrees with task records: {label}")
    inbox = getattr(agent, "_inbox", None)
    if inbox is not None and hasattr(inbox, "record_event"):
        inbox.record_event("status_mismatch", model=label, role="foreground")


def verify_reply(agent, messages, chat, result):
    """Record the reply's declared question as a decision, and log any declared status that the task records
    contradict. Status itself comes from the records (live_context is attached to every turn before the agent
    writes); since 2026-10-21 no second model reads the reply to guess at its claims (Codex review item 3)."""
    claims = list(getattr(result, "status_claims", []))
    if getattr(result, "question", "") and not agent.BACKGROUND_TASK_ID:
        ident = int(getattr(result, "decision_task_id", 0))
        if not ident:
            waiting = {c["task_id"] for c in claims if c["state"] == "waiting"}
            if len(waiting) == 1:
                ident = waiting.pop()
        if ident and not agent.BACKGROUND_TASK_ID:
            try:
                if agent._background.get(ident)["status"] in ("finished", "cancelled", "superseded"):
                    ident = 0                   # asking never reopens a task (2026-10-21); reopening is resume
            except ValueError:
                ident = 0
        if ident and not (agent.BACKGROUND_TASK_ID and agent._background.pending_controls(ident)):
            existing = [d for d in agent._background.decisions(chat, ident) if d.get("kind", "question") == "question"]
            result.decision_id = (existing[0] if existing else agent._background.ask(chat, ident, result.question))["id"]
        elif not ident:
            # A conversation question has no executable task. Its answer never queues invented work.
            existing = [d for d in agent._background.decisions(chat, 0) if d.get("kind", "question") == "question"]
            result.decision_id = (existing[0] if existing else agent._background.ask(chat, 0, result.question))["id"]
    mismatches = []
    own = int(agent.BACKGROUND_TASK_ID or 0)
    for claim in claims:
        # A worker drafting its answer is running by definition; what it says about its own task is its outcome,
        # recorded by finish(), not a claim to check against its live state (2026-10-20, task 234).
        if own and str(claim.get("task_id")) == str(own):
            continue
        try:
            actual = agent._background.live(chat, int(claim["task_id"]))
            if actual["state"] != claim["state"]:
                mismatches.append(actual)
        except (ValueError, KeyError, TypeError):
            mismatches.append({"task_id": claim.get("task_id"), "state": "unverified"})
    if mismatches:
        _log_mismatch(agent, mismatches)        # the reply goes out exactly as the agent wrote it
    # The providers save the draft before returning. Keep history consistent with what is actually delivered.
    if messages and messages[-1].get("role") == "assistant":
        messages[-1]["content"] = result
        agent.save_conversation(chat)
    return result


def migrate_questions(store):
    """Existing explicit waiting states predate the ledger; import once, without guessing from prose."""
    db = sqlite3.connect(store.database, timeout=30)
    try:
        rows = list(db.execute("SELECT id,chat FROM tasks WHERE status='needs_answer'"))
    finally:
        db.close()
    for ident, chat in rows:
        if not store.decisions(chat, ident):
            question = store.legacy_question(ident)
            if question:
                store.ask(chat, ident, question)
