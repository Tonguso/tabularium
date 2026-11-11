"""Durable decisions and evidence-backed task reporting, shared by every agent."""
import json
import sqlite3
import time
from pathlib import Path
from datetime import datetime
from tabularium.core.outcome import Failed
from tabularium.records.worker_lease import busy
from tabularium.records import lifecycle
TOOL = {
    "name": "task_decision",
    "description": "Persist a task's question BEFORE asking it; list unanswered decisions; answer using an exact user quote. When Alex's current message is the answer, give decision_id and the quote and leave source_id out; otherwise give source_id from search_conversations. A Telegram reply identifies a candidate question only. Record an answer only when his words actually resolve it; clarification and unrelated instructions leave it pending. Answer queues continuation atomically. Never substitute an assistant suggestion for the user's decision. For an exact action answering an existing question, prepare with its decision_id, tool and inputs: this reuses that decision and requests exact approval. For a genuinely unrelated action use decision_id=0 explicitly. Prepare does not execute or approve anything. Parked actions are approved only by his own yes.",
    "input_schema": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["list", "ask", "answer", "prepare"]},
        "task_id": {"type": "integer"}, "question": {"type": "string"},
        "decision_id": {"type": "integer"}, "answer": {"type": "string"},
        "source_id": {"type": "integer"}, "quote": {"type": "string"},
        "tool": {"type": "string", "enum": ["send_email", "execute_command"]},
        "inputs": {"type": "object"}},
        "required": ["action"]},
}


RULING_SECONDS = 48 * 3600
LAPSED_SECONDS = 7 * 24 * 3600


def pending(store, chat, task=None, kind=None):
    db = sqlite3.connect(store.database, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        query = "SELECT decisions.*, (SELECT status FROM tasks WHERE tasks.id=decisions.task) AS task_state FROM decisions WHERE chat=? AND state='pending'"
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
    return rows


def lapse(store, chat, before, keep=()):
    """A conversation question (task 0) lives until the owner's next message in the chat. When that message arrives
    and does not identify the question (a reply to it, or the dashboard's decision id), the question lapses: it
    leaves the per-turn evidence, stays in the record, and a later quoted answer still settles it. A task's question
    never lapses; the task waits on it. (2026-11-11: 99 pending conversation questions, 60 older than three days,
    rode every turn as 36,000 characters of evidence, most of them answered in the conversation long before.)"""
    keep = [int(k) for k in keep if k]
    db = sqlite3.connect(store.database, timeout=30)
    try:
        db.execute("BEGIN IMMEDIATE")
        sql = ("UPDATE decisions SET state='lapsed',updated=? WHERE chat=? AND task=0 AND kind='question' "
               "AND state='pending' AND created<?")
        args = [time.time(), chat, float(before)]
        if keep:
            sql += " AND id NOT IN (" + ",".join("?" * len(keep)) + ")"
            args += keep
        changed = db.execute(sql, args).rowcount
        db.commit()
        return changed
    finally:
        db.close()


def lapsed(store, chat, since=None):
    """Conversation questions the owner has moved past without answering, newest last; `since` limits by creation."""
    db = sqlite3.connect(store.database, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in db.execute(
            "SELECT * FROM decisions WHERE chat=? AND task=0 AND kind='question' AND state='lapsed' AND created>? ORDER BY id",
            (chat, float(since or 0)))]
    finally:
        db.close()


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
        if ident and store.worker_attempt and (not task or task["attempt"] != store.worker_attempt):
            raise ValueError("Worker no longer owns this task.")
        query = "SELECT * FROM decisions WHERE task=? AND chat=? AND state='pending'"
        args = [ident, chat]
        if not ident:
            query += " AND kind='question' AND question=?"
            args.append(question)
        old = db.execute(query, args).fetchone()
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


def park_action(store, chat, tool, inputs, question, decision_id=None, task=0):
    """Attach the exact action to its existing question; never guess the relationship."""
    now = time.time()
    db = sqlite3.connect(store.database, timeout=30)
    db.row_factory = sqlite3.Row
    action = json.dumps({"tool": tool, "inputs": inputs}, ensure_ascii=False)
    try:
        db.execute("BEGIN IMMEDIATE")
        if decision_id is None and db.execute(
                "SELECT 1 FROM decisions WHERE chat=? AND state='pending'", (chat,)).fetchone():
            raise ValueError("Use task_decision prepare with the originating decision_id, or decision_id=0 for an unrelated action.")
        if decision_id:
            row = db.execute("SELECT * FROM decisions WHERE id=? AND chat=?", (decision_id, chat)).fetchone()
            if not row or row['state'] != 'pending':
                raise ValueError("The originating decision is not pending in this chat.")
            if row['kind'] == 'action':
                if row['action'] != action:
                    raise ValueError("This decision already has a different exact action awaiting approval.")
                return dict(row)
            task = row['task']
            if task:
                state = db.execute("SELECT status FROM tasks WHERE id=?", (task,)).fetchone()[0]
                if state in ('cancelled', 'cancelling', 'superseded', 'finished'):
                    raise ValueError("The task is no longer available for this action.")
            # Old question messages cannot authorize a newly specified exact action.
            db.execute("UPDATE decisions SET kind='action',action=?,message_ids='[]',updated=? WHERE id=?",
                       (action, now, decision_id))
            key = decision_id
        else:
            if task:
                raise ValueError("Create the task question first, then prepare its exact action by decision_id.")
            key = db.execute("INSERT INTO decisions(task,chat,question,created,updated,kind,action) "
                             "VALUES (0,?,?,?,?,'action',?)", (chat, question, now, now, action)).lastrowid
        db.commit()
        return dict(db.execute("SELECT * FROM decisions WHERE id=?", (key,)).fetchone())
    finally:
        db.close()


def bind_messages(store, decision, message_ids):
    """Record the Telegram messages that asked decision `decision`; a reply to any of them identifies it as a candidate."""
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
    """The decision that Telegram message `message_id` asked, pending or lapsed, or None."""
    for row in pending(store, chat) + lapsed(store, chat):
        if int(message_id) in json.loads(row.get("message_ids") or "[]"):
            return row
    return None


def settle_action(store, chat, decision, approved, source, outcome=""):
    """Claim approval once before execution. An interrupted claim has an unknown outcome, never an automatic retry."""
    db = sqlite3.connect(store.database, timeout=30)
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT task FROM decisions WHERE id=? AND chat=? AND state='pending' AND kind='action'",
                         (decision, chat)).fetchone()
        if not row:
            return False
        if row[0]:
            state = db.execute("SELECT status FROM tasks WHERE id=?", (row[0],)).fetchone()
            if not state or state[0] in ('cancelled', 'cancelling', 'superseded', 'finished'):
                db.execute("UPDATE decisions SET state='declined',answer='Task ended; action not executed.',updated=? WHERE id=?",
                           (time.time(), decision))
                db.commit()
                return False
        changed = db.execute("UPDATE decisions SET state=?,answer=?,source=?,updated=? WHERE id=? AND chat=? "
                             "AND state='pending' AND kind='action'",
                             ("answered" if approved else "declined", "Outcome unknown until execution is recorded." if approved else "Declined; not executed.",
                              source, time.time(), int(decision), chat)).rowcount
        if changed and approved and row[0]:
            lifecycle.move(db, row[0], 'stop', 'needs_attention',
                           'Approved action claimed; outcome unknown until recorded', notified=1)
        db.commit()
        return bool(changed)
    finally:
        db.close()


def finish_action(store, chat, decision, outcome):
    """Save the tool outcome and queue the original task in the same transaction."""
    db = sqlite3.connect(store.database, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM decisions WHERE id=? AND chat=? AND kind='action'", (decision, chat)).fetchone()
        if not row or row['state'] not in ('answered', 'declined'):
            raise ValueError("Action has no recorded approval or refusal.")
        action = json.loads(row['action'])
        if 'result' in action:
            return
        action['result'] = str(outcome)
        db.execute("UPDATE decisions SET answer=?,action=?,updated=? WHERE id=?",
                   (str(outcome), json.dumps(action, ensure_ascii=False), time.time(), decision))
        task = db.execute("SELECT status FROM tasks WHERE id=?", (row['task'],)).fetchone()
        if task and task[0] not in ('cancelled', 'cancelling', 'superseded', 'finished'):
            control = db.execute("INSERT INTO controls(task,text) VALUES (?,?)", (row['task'],
                f"Decision #{decision}: {row['question']}\nRecorded action outcome: {outcome}\n"
                "Use this result; do not repeat the action. Continue the original task from its saved evidence.")).lastrowid
            db.execute("UPDATE decisions SET control=? WHERE id=?", (control, decision))
            state = lifecycle.WAITING if task[0] == lifecycle.WAITING else 'queued'
            lifecycle.move(db, row['task'], 'answer', state, 'Action outcome recorded; continuation queued')
        db.commit()
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
    ledger = sqlite3.connect(store.database, timeout=30)
    try:
        decision_row = ledger.execute("SELECT created FROM decisions WHERE id=? AND chat=?", (decision, chat)).fetchone()
    finally:
        ledger.close()
    if decision_row:
        if not row[3] or datetime.fromisoformat(row[3]).timestamp() + 60 < decision_row[0]:
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
        if row["state"] != "pending" and not (row["task"] == 0 and row["state"] == "lapsed"):
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
    from tabularium.records import verification
    from tabularium.learning import regression
    from tabularium.learning.corrections import is_diagnosis
    task = store.get(ident)
    reasons, items = [], []
    record_only = bool(context.get("trial") or is_diagnosis(task["request"]))
    if record_only:
        return dict(passed=False, verdict="recorded", reasons=["Record saved; not verified task completion."], items=[])
    if getattr(result, "task_outcome", "none") != "completed" or not getattr(result, "complete", False):
        reasons.append("The worker did not declare a complete result.")
    if (getattr(result, "question", "") and getattr(result, "task_outcome", "none") != "completed") or checkpoint.get("blocker"):
        reasons.append("An unresolved question or checkpoint blocker remains.")
    benchmark = (context.get("eval") or {}).get("benchmark")
    if benchmark:
        try:
            regression.bound_case(store.agent_dir, context)
        except ValueError as exc:
            reasons.append(str(exc))
    else:
        # No bookkeeping test here (2026-10-24, Alex: [quote removed]): whether every instruction carries a
        # declared requirement is guidance in the worker's context, never evidence that work is incomplete. The
        # runtime's own resume note (control 235 on task 348) was counted as an uncovered instruction.
        requirements, _ = verification.contract(store, ident)
        try:
            verification.validate_items(requirements)
            # A requirement without a declared check is the owner's to judge by reading; it never blocks finishing
            # (2026-10-24). A declared check must have passed on the current file.
            items = [dict(id=n, item=it["item"], status="judgment" if verification.is_judgment(it)
                          else verification.status(store, ident, "", n))
                     for n, it in enumerate(requirements, 1)]
            for it in items:
                if it["status"] not in ("passed", "judgment"):
                    reasons.append(f"Check {it['status']}: {it['item']}.")
        except (ValueError, TypeError, KeyError) as exc:
            reasons.append("Completion requirements unavailable: " + str(exc))
    # The same delivery-bound acceptance decision is used here and at review.
    for delegation in store.delegations(ident):
        if delegation['status'] == 'cancelled':
            continue
        request = delegation['request_id']
        try:
            _, _, unmet = verification.acceptance(store, ident, request)
            if unmet:
                reasons.append(f"Delegated requirements not accepted ({request}): {unmet}.")
        except (ValueError, TypeError, KeyError, OSError) as exc:
            reasons.append(f"Dependency {request}: {exc}")
    # Benchmark generation is recorded here; its independent evaluator owns artifact and subjective grading.
    verdict = ("recorded" if benchmark else "passed") if not reasons else "unverified"
    return dict(passed=not reasons and not benchmark, verdict=verdict, reasons=reasons, items=items)


def finish(store, ident, result, *, commit=True, notes=None):
    """One decision, previewed before the final reply and rechecked when committing task state. `notes` are the
    runtime's own bookkeeping for the dashboard's task segment (route, the delegate's account, a repeated lesson):
    they never join the text Alex receives (2026-10-26 and 2026-10-31, Alex on COST and [Route] lines)."""
    from tabularium.core.agent_runtime import atomic_json
    from tabularium.memory.memory_store import atomic_text
    from tabularium.learning.corrections import is_diagnosis
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
    if store.worker_attempt and task['attempt'] != store.worker_attempt:
        raise ValueError("Worker no longer owns this task.")
    decision = 0
    if commit and not isolated and task['status'] not in ('cancelled', 'cancelling', 'superseded', 'finished', 'queued'):
        # A completed assignment may propose new work; its question must not reopen this assignment.
        question = getattr(result, 'question', '')
        if question:
            owner = 0 if getattr(result, 'task_outcome', 'none') == 'completed' else ident
            decision = declare_question(store, task['chat'], result, owner)
        elif getattr(result, 'task_outcome', 'none') == 'waiting':
            question = store.legacy_question(ident)
            if question:
                decision = store.ask(task['chat'], ident, question)['id']
    # One inexpensive dependency refresh, never another model turn or a polling loop.
    if commit and store.unread_delegations(ident):
        store.refresh_delegations(ident)
    from tabularium.records import verification
    try:
        verification.saved_items(store, ident)
        for dependency in store.delegations(ident):
            verification.saved_items(store, ident, dependency["request_id"])
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        errors.append("Requirement contract unavailable: " + str(exc))
    db = sqlite3.connect(store.database, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN IMMEDIATE" if commit else "BEGIN")
        task = db.execute("SELECT * FROM tasks WHERE id=?", (ident,)).fetchone()
        if store.worker_attempt and task["attempt"] != store.worker_attempt:
            raise ValueError("Worker no longer owns this task.")
        if not commit and task["status"] != "running":
            return dict(state=task["status"], reasons=[])
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
        evidence.update(state=state, checked_at=time.time(), decision_id=decision)
        if state != "finished" or errors:
            evidence.update(passed=False, verdict="unverified")
        if not commit:
            return evidence
        evidence.update(notes or {})
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


def completion_feedback(agent, chat, result):
    """Give a repairable final draft its existing completion evidence while tools are still available."""
    from tabularium.core.reply_envelope import retry_note
    if (not agent.BACKGROUND_TASK_ID or not getattr(result, 'complete', False)
            or getattr(result, 'task_outcome', 'none') in ('blocked', 'waiting')):
        return None
    decision = finish(agent._background, int(agent.BACKGROUND_TASK_ID), result, commit=False)
    if decision['state'] != 'needs_attention' or not decision['reasons']:
        return None
    reasons = tuple(decision['reasons'])
    seen = agent._completion_feedback.setdefault(chat, [])
    # Two repair turns at most; repeating the same failure is not progress. Kept across session limits.
    if reasons in seen or len(seen) >= 2:
        return None
    seen.append(reasons)
    return retry_note(agent, 'completion', result,
                      'Completion evidence: ' + '; '.join(reasons) +
                      '\nRepair these specific gaps using the existing artifacts and tools. Preserve valid proof; '
                      'do not repeat completed actions. If work is complete, declare task_outcome=completed. '
                      'If a real blocker prevents repair, record it and declare blocked or ask the needed question. '
                      'These diagnostics are runtime feedback, not a message from Alex.')


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
    db.row_factory = sqlite3.Row
    try:
        ids = [r[0] for r in db.execute(
            "SELECT id FROM tasks WHERE chat=? AND status NOT IN ('finished','cancelled','superseded') "
            "AND (? IS NULL OR id=?) ORDER BY id", (chat, task, task))]
        outcomes = [dict(r) for r in db.execute(
            "SELECT id,task,question,state,answer FROM decisions WHERE chat=? AND kind='action' AND state<>'pending' "
            "AND (? IS NULL OR task=?) ORDER BY updated DESC LIMIT 6", (chat, task, task))]
        # Alex's recent answers to this chat's questions, whichever turn or worker asked them: a ruling binds every
        # later question, so no one offers again what he refused (2026-11-08: the same "delete the left piece"
        # option came back four times in one morning, from the foreground and from a worker that never saw his no).
        rulings = [dict(id=r['id'], question=r['question'][:300], answer=r['answer'][:300]) for r in db.execute(
            "SELECT id,question,answer FROM decisions WHERE chat=? AND kind='question' AND state<>'pending' "
            "AND answer IS NOT NULL AND answer<>'' AND updated>? ORDER BY updated DESC LIMIT 12",
            (chat, time.time() - RULING_SECONDS))]
    finally:
        db.close()
    decisions = store.decisions(chat, task)
    unanswered = len(lapsed(store, chat, since=time.time() - LAPSED_SECONDS)) if task is None else 0
    if not ids and not decisions and not outcomes and not rulings and not unanswered:
        return ""
    for outcome in outcomes:
        outcome['answer'] = outcome['answer'][:1500] if outcome['answer'] else 'Outcome unknown.'
    evidence = {"tasks": [store.live(chat, i) for i in ids], "pending_decisions": decisions}
    if outcomes:
        evidence['action_outcomes'] = outcomes
    if rulings:
        evidence['owner_rulings'] = {'note': "Alex's answers in this chat over the last two days, newest first. They bind "
                                             "every later question here and in every worker: never offer again what he refused.",
                                     'answers': rulings}
    if unanswered:
        evidence['questions_not_answered'] = {'count': unanswered, 'note': "Conversation questions of the last week that Alex "
                                              "moved past without answering; not shown here (task_decision list has them). "
                                              "Ask one again only when its matter is live, never as a reminder."}
    return "[Live task evidence; historical assistant claims are not current status]\n" + json.dumps(evidence, ensure_ascii=False)


def execute(agent, chat, inputs):
    try:
        action = inputs["action"]
        store = agent._background
        if action == "list":
            result = store.decisions(chat) + lapsed(store, chat, since=time.time() - LAPSED_SECONDS)
        elif action == "ask":
            ident = int(inputs.get("task_id") or agent.BACKGROUND_TASK_ID or 0)
            if agent.BACKGROUND_TASK_ID and ident != int(agent.BACKGROUND_TASK_ID):
                raise ValueError("A worker can only ask about its own task.")
            result = store.ask(chat, ident, inputs["question"])
        elif action == "prepare":
            if agent.BACKGROUND_TASK_ID:
                raise ValueError("The foreground prepares exact actions against the worker's decision_id.")
            tool, values = inputs["tool"], inputs["inputs"]
            schemas = {(t.get('function') or t)['name']: (t.get('function') or t)
                       for t in agent.get_tools('anthropic')}
            if tool not in ('send_email', 'execute_command') or tool not in schemas:
                raise ValueError("This agent cannot prepare that tool.")
            schema = schemas[tool]['input_schema']
            if not isinstance(values, dict) or set(values) - set(schema.get('properties', {})):
                raise ValueError("Unknown action inputs.")
            if any(k not in values for k in schema.get('required', [])):
                raise ValueError("Missing required action inputs.")
            return agent._park(chat, tool, values, "Approve this exact action:\n" + json.dumps(
                {"tool": tool, "inputs": values}, ensure_ascii=False), decision_id=int(inputs['decision_id']))
        elif action == "answer":
            if agent.BACKGROUND_TASK_ID:
                raise ValueError("The foreground agent records user answers.")
            if inputs.get("source_id"):
                result = store.answer(chat, inputs["decision_id"], inputs["answer"], inputs["source_id"], inputs["quote"])
            else:
                result = answer_from_current(agent, chat, inputs["decision_id"], inputs["answer"], inputs["quote"])
        else:
            raise ValueError("Use list, ask, answer or prepare.")
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


def declaration_error(store, chat, result, task):
    """Why the reply's declared decision cannot be bound, or None. A pure check: the turn loop bounces the draft on
    it once, and verify_reply falls back to a new question instead of ending the turn (2026-10-27 23:27, the
    refusal text reached Alex as "Task stopped")."""
    decision = int(getattr(result, 'decision_id', 0))
    if not decision:
        return None
    if not getattr(result, 'question', ''):
        return "A decision binding must declare the question it asks."
    row = next((d for d in store.decisions(chat) if d['id'] == decision), None)
    if not row or row['task'] != task:
        return "Declared decision is not pending for this chat and task."
    if row['question'] != result.question:
        return "Declared decision asks a different question; use its exact question or create a new decision."
    return None


DECISION_NOTE = ("[System: {error} Set decision_id to the pending decision whose question you repeat word for word, "
                 "or decision_id 0 for a new question.]")


def declare_question(store, chat, result, task):
    """Bind only the declared decision, or create this exact new question. Never infer from task status."""
    error = declaration_error(store, chat, result, task)
    if error:
        raise ValueError(error)
    decision = int(getattr(result, 'decision_id', 0))
    row = next(d for d in store.decisions(chat) if d['id'] == decision) if decision else store.ask(chat, task, result.question)
    result.decision_id = row['id']
    return row['id']


def verify_reply(agent, messages, chat, result):
    """Record the reply's declared question as a decision, and log any declared status that the task records
    contradict. Status itself comes from the records (live_context is attached to every turn before the agent
    writes); since 2026-10-21 no second model reads the reply to guess at its claims (Codex review item 3)."""
    claims = list(getattr(result, "status_claims", []))
    if not agent.BACKGROUND_TASK_ID and (getattr(result, "question", "") or getattr(result, "decision_id", 0)):
        task = int(getattr(result, 'decision_task_id', 0))
        error = declaration_error(agent._background, chat, result, task)
        if error:
            # The draft was already bounced once in the turn loop; a wrong declaration is recorded, never a crash.
            print(f"[decision] declared decision_id {getattr(result, 'decision_id', 0)} dropped: {error}")
            agent._inbox.record_event("status_mismatch", model="decision_id", role="foreground")
            result.decision_id = 0
        if getattr(result, "question", ""):
            declare_question(agent._background, chat, result, task)
    if not agent.BACKGROUND_TASK_ID:
        _lapse_conversation_questions(agent, chat, int(getattr(result, "decision_id", 0) or 0))
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


def _lapse_conversation_questions(agent, chat, declared):
    """The owner's message this turn answered closes the earlier conversation questions it did not identify: the
    one this reply declares, one he replied to, one the dashboard named."""
    inbox = agent._inbox
    if not hasattr(inbox, "turn_sources") or not inbox.answering():
        return
    try:
        sources = inbox.turn_sources(chat)
        if not sources:
            return
        before = min(float(s.get("date") or time.time()) for s in sources)
        keep = {declared}
        for s in sources:
            ref = (s.get("reply_to_message") or {}).get("message_id")
            if ref:
                row = by_message(agent._background, chat, ref)
                if row:
                    keep.add(row["id"])
            if s.get("decision_id"):
                keep.add(int(s["decision_id"]))
        lapse(agent._background, chat, before, keep)
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        print(f"[decision] lapse: {type(exc).__name__}: {exc}")


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
