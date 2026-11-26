"""One task lifecycle: every status change is a named operation, checked against this table (2026-10-21).

Until today task status was written by thirteen UPDATE statements and a generic setter in two files, and nothing
said which moves were allowed. In an isolated reproduction, asking a question about a finished task silently put it
back to needs_answer. Now each operation names the states it may start from and the states it may lead to; a move
outside the table raises InvalidTransition and changes nothing. Reopening a finished task is its own operation.

move() runs inside the caller's transaction and updates only if the status is still the one it read, so two
processes cannot both make a move from the same state.

Every move is also an event (2026-11-23). The task row kept only its latest status and note, and facts about a
task's life were kept beside it in files and tables that nothing tied to the move: the completion verdict in
completion.json, written before the move committed and never touched when the task was later resumed; the time it
parked in context.json; its activations in their own table. The events table is the task's history, appended by the
same statement path that changes the status, in the same transaction, so the two cannot disagree. A fact that
belongs to a transition travels as that event's data and is read from there; it has no other copy.
"""
import json
import time

WAITING = "waiting_on_delegate"
STOPPED = ("needs_attention", "interrupted", "capped", "infeasible")

OPS = {
    # operation: (allowed current states, allowed results)
    "claim":   ({"queued"}, {"starting"}),                                   # a launch slot is reserved
    "defer":   ({"starting"}, {"queued"}),                                   # draining: give the slot back
    "run":     ({"starting"}, {"running"}),                                  # the worker process owns it
    # waits for Cal or Vit; also from needs_answer: a worker's draft can record a question for Alex and, in the same
    # delivery, find a delegate still in flight (task 264, 2026-10-22). The question stays pending while it waits.
    "park":    ({"starting", "running", "needs_answer"}, {WAITING}),
    "wake":    ({WAITING}, {"queued"}),                                      # the delegate returned or asked
    "ask":     ({"queued", "starting", "running", "needs_answer", WAITING, *STOPPED}, {"needs_answer"}),
    # answered while parked on a delegate: the answer is recorded, the task keeps waiting (2026-10-22, task 264)
    "answer":  ({"needs_answer", "queued", "starting", "running", WAITING, *STOPPED}, {"queued", WAITING}),
    "finish":  ({"starting", "running", "queued", "needs_answer"}, {"finished", "needs_attention", "needs_answer", "queued"}),
    "stop":    ({"queued", "starting", "running", "needs_answer", WAITING}, {"interrupted", "needs_attention"}),
    "recover": ({"interrupted"}, {"queued"}),                                # crash with nothing half-done
    "resume":  ({"needs_answer", WAITING, "cancelled", *STOPPED}, {"queued"}),
    "reopen":  ({"finished"}, {"queued"}),                                   # explicit: more work on a finished task
    "cancel":  ({"queued", "starting", "running", "cancelling", "pausing", "needs_answer", WAITING, *STOPPED},
                {"cancelling", "cancelled"}),
    # a release drains: the worker stops at its next safe point and the task is queued for the new process
    # (2026-11-14: a release waited an hour behind one engineering worker, twice)
    "pause":   ({"starting", "running"}, {"pausing"}),
    "paused":  ({"pausing"}, {"queued"}),
}

ACTIVATIONS = ("start", "resume", "reopen")        # the operations a reply's "work started" is checked against
EVENTS = ("CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, task INTEGER NOT NULL, at REAL NOT NULL, "
          "op TEXT NOT NULL, status TEXT NOT NULL, note TEXT, data TEXT)")

_ANY = object()


def record(db, ident, op, status, note=None, data=None, at=None):
    """Append one event to task `ident`'s history, inside the caller's transaction."""
    db.execute("INSERT INTO events(task,at,op,status,note,data) VALUES (?,?,?,?,?,?)",
               (int(ident), time.time() if at is None else at, op, status, note,
                None if data is None else json.dumps(data, ensure_ascii=False)))


def born(db, chat, request, status, note, *, notified=0):
    """Create a task with its first event; the one way a task row comes to exist. Returns its id."""
    now = time.time()
    ident = db.execute("INSERT INTO tasks(chat,request,status,created,updated,note,notified) VALUES (?,?,?,?,?,?,?)",
                       (chat, request, status, now, now, note, notified)).lastrowid
    record(db, ident, "start", status, note, at=now)
    return ident


def history(db, ident):
    """The task's events, oldest first: [{at, op, status, note, data}]."""
    rows = db.execute("SELECT at,op,status,note,data FROM events WHERE task=? ORDER BY id", (int(ident),)).fetchall()
    return [dict(at=r[0], op=r[1], status=r[2], note=r[3], data=json.loads(r[4]) if r[4] else None) for r in rows]


def completion(db, ident):
    """The completion verdict the task stands on now: the data of its newest event, or None when the task has moved
    on since it was judged (resumed, reopened, answered) or was never judged."""
    row = db.execute("SELECT data FROM events WHERE task=? ORDER BY id DESC LIMIT 1", (int(ident),)).fetchone()
    return json.loads(row[0]) if row and row[0] else None


def since(db, ident, status):
    """When the task last entered `status`, or None."""
    row = db.execute("SELECT at FROM events WHERE task=? AND status=? ORDER BY id DESC LIMIT 1", (int(ident), status)).fetchone()
    return row[0] if row else None


def adopt(db, completion_of):
    """Give every task that predates the journal one event made from its row, with the completion verdict its folder
    held (completion_of(task id) -> dict or None). Runs at store start; a task with any event is left alone."""
    rows = db.execute("SELECT id,status,note,updated FROM tasks WHERE id NOT IN (SELECT task FROM events)").fetchall()
    for ident, status, note, updated in rows:
        record(db, ident, "adopt", status, note, completion_of(ident), at=updated)
    return len(rows)


def set_aside(db, chat, before):
    """A stopped task lives in the per-turn evidence until the owner's next message has been answered; if it is still
    stopped then, it is set aside: out of the evidence, in the record, resumable, listed by background_task status.
    The same rule a conversation question has had since 2026-11-11, for the same reason. (2026-11-26: 33 stopped
    tasks, the oldest four weeks old, rode every turn of the coordinator; a stopped state had no end at all, so
    each one was listed until someone cancelled it, and no one's job was to cancel it.) A task waiting on his
    answer is never set aside; it waits. Returns how many were set aside."""
    marks = ",".join("?" * len(STOPPED))
    return db.execute(f"UPDATE tasks SET set_aside=? WHERE chat=? AND set_aside IS NULL AND updated<? AND status IN ({marks})",
                      (time.time(), chat, float(before), *STOPPED)).rowcount


class InvalidTransition(ValueError):
    """The move is not in the lifecycle table, or the task changed state underneath it. Nothing was written."""


def allowed(op, current, to):
    sources, targets = OPS[op]
    return current in sources and to in targets


def move(db, ident, op, to, note=None, *, notified=0, quiet=False, attempt_is=_ANY, data=None, **fields):
    """Make operation `op` take task `ident` to status `to`. note=None keeps the note; notified=None keeps the flag;
    fields are extra columns (pid, attempt); data is what this transition established (a completion verdict) and is
    kept with its event. quiet=True returns False instead of raising: for monitors that only act if the task is
    still where they last saw it. Returns True when the move was made."""
    if op not in OPS or to not in OPS[op][1]:
        raise InvalidTransition(f"{op} cannot lead to {to}.")
    row = db.execute("SELECT status FROM tasks WHERE id=?", (int(ident),)).fetchone()
    if row is None:
        raise ValueError("Unknown background task.")
    current = row[0]
    if current not in OPS[op][0]:
        if quiet:
            return False
        raise InvalidTransition(f"Task {ident} is {current}; {op} applies only to "
                                f"{', '.join(sorted(OPS[op][0]))}.")
    columns, values = ["status=?", "updated=?", "set_aside=NULL"], [to, time.time()]     # any move brings it back
    if note is not None:
        columns.append("note=?"); values.append(note)
    if notified is not None:
        columns.append("notified=?"); values.append(notified)
    for name, value in fields.items():
        columns.append(f"{name}=?"); values.append(value)
    where, args = "id=? AND status=?", [int(ident), current]
    if attempt_is is not _ANY:
        where += " AND attempt IS ?"; args.append(attempt_is)
    changed = db.execute(f"UPDATE tasks SET {','.join(columns)} WHERE {where}", values + args).rowcount
    if not changed and not quiet:
        raise InvalidTransition(f"Task {ident} changed state during {op}; nothing written.")
    if changed:
        record(db, ident, op, to, note, data, at=values[1])
    return bool(changed)
