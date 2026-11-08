"""One task lifecycle: every status change is a named operation, checked against this table (2026-10-21).

Until today task status was written by thirteen UPDATE statements and a generic setter in two files, and nothing
said which moves were allowed. In an isolated reproduction, asking a question about a finished task silently put it
back to needs_answer. Now each operation names the states it may start from and the states it may lead to; a move
outside the table raises InvalidTransition and changes nothing. Reopening a finished task is its own operation.

move() runs inside the caller's transaction and updates only if the status is still the one it read, so two
processes cannot both make a move from the same state.
"""
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
    "cancel":  ({"queued", "starting", "running", "cancelling", "needs_answer", WAITING, *STOPPED},
                {"cancelling", "cancelled"}),
}

_ANY = object()


class InvalidTransition(ValueError):
    """The move is not in the lifecycle table, or the task changed state underneath it. Nothing was written."""


def allowed(op, current, to):
    sources, targets = OPS[op]
    return current in sources and to in targets


def move(db, ident, op, to, note=None, *, notified=0, quiet=False, attempt_is=_ANY, **fields):
    """Make operation `op` take task `ident` to status `to`. note=None keeps the note; notified=None keeps the flag;
    fields are extra columns (pid, attempt). quiet=True returns False instead of raising: for monitors that only act
    if the task is still where they last saw it. Returns True when the move was made."""
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
    columns, values = ["status=?", "updated=?"], [to, time.time()]
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
    return bool(changed)
