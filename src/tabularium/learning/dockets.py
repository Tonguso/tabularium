"""Observations, dockets and method reviews ("Lessons That Change Methods", docs/design, 2026-11-10).

A recorded failure is an observation: the lesson record as a candidate, never merged away or deleted by grouping. A
docket is a stored record (kind 'docket' in the agent's project store, written only here, never through the model's
project_record tool): a method file, a cause claim, the members the owning agent confirmed and the unreviewed members
the similarity measure proposed. Membership is written only by the owning agent's lesson(action='group' / 'ungroup');
the runtime proposes it from the same bigram measure the digest used.

Each observation carries one review state on its own record (`review`): none until the runtime proposes it into a
docket; then the docket, the time and a verdict, 'proposed' until the owning agent rules, 'member' when grouped,
'excluded' when ungrouped. The proposer sees only observations with no review state, so a judgement is never
re-proposed and ungrouped observations are never re-clustered on their own (2026-11-10 evening: without this state the
minute loop re-attached what an agent had just ungrouped and reviewed one docket ten times). THRESHOLD observations
within WINDOW_DAYS (the owner's setting: three in sixty days) open a method review: one task in the task store's
runtime lane, keyed by the docket, asking the owner agent for the cause and the edit for the docket as a whole. A docket
is reviewed again only when THRESHOLD observations have been proposed into it since its last review started. A docket
closes when a proposal explaining its members is applied, or when the agent ungroups every member.
"""
from contextlib import closing
import hashlib
import json
import sqlite3
import time
from pathlib import Path

from tabularium.learning import learning

THRESHOLD, WINDOW_DAYS = 3, 60
SIMILARITY = 0.3
PREFIX = "[Method review]"
ACTIVE = ("queued", "starting", "running", "cancelling")
BRIEF = """{prefix} Docket {docket}: {n} observations of one failure within {window} days.
Method file: {method}
Observations (key; when; text):
{items}
This is one review of the docket, not one lesson per observation. Nothing you write here is sent to the owner; he reads
the register and the dashboard when he chooses. Do this:
1. Read the method file and each observation's record (project_record read kind=lesson key=<key>; a task checkpoint
   path in its source names that task's records).
2. Decide which observations share one cause. Confirm membership with lesson(action='group', docket='{docket}',
   members=[...], method_path=<the method file>); move one out with lesson(action='ungroup', docket='{docket}',
   members=[...], note=<why>). An observation you move out is judged: the runtime will not propose it again.
3. Name the decision in the method that allowed every member, and the edit that removes it: lesson(action='propose',
   key=<one member>, method_path, cause={{claim, anchor quoted exactly from the method or silent_at}},
   explains=[the other members], current_text, new_text). The edit rewrites the anchored rule; a rule that must be
   remembered means the method still permits the failure.
4. If the members share no cause, ungroup them with the reason and propose nothing.
Your final reply is the record, at most eight short lines: the docket, the cause, the proposal key or 'no common cause'.
Ask no question: nobody will answer it. Declare task_outcome completed.
"""


def _connect(root):
    return learning.ProjectStore(Path(root).resolve()).connect()


def _docket_key(first):
    return "docket-" + hashlib.sha256(str(first).encode()).hexdigest()[:12]


def _save(root, chat, key, data):
    """Write one docket record, keeping the previous version in history (the model has no path to this table)."""
    with closing(_connect(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        rec = db.execute("SELECT * FROM records WHERE chat=? AND kind='docket' AND key=?", (chat, key)).fetchone()
        if rec:
            db.execute("INSERT INTO history VALUES(?,?,?,?,?,?)", tuple(rec))
        data["updated"] = time.time()
        db.execute("INSERT OR REPLACE INTO records VALUES(?,?,?,?,?,?)",
                   (chat, "docket", key, (rec["version"] + 1) if rec else 1, json.dumps(data, ensure_ascii=False), data["updated"]))
    return data


def _mark(root, chat, key, docket, verdict, note="", at=None):
    """The observation's own review state: which docket, when, and the verdict."""
    state = {"docket": docket, "at": at or time.time(), "verdict": verdict}
    if note:
        state["note"] = " ".join(str(note).split())
    learning._update(root, chat, key, lambda d: d.update(review=state))
    return state


def dockets(root, chat, statuses=None):
    """[(key, data)] of this agent's dockets, newest first; `statuses` narrows."""
    path = Path(root) / "task_state/projects.sqlite3"
    if not path.exists():
        return []
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='records'").fetchone():
            return []
        rows = db.execute("SELECT key,data FROM records WHERE chat=? AND kind='docket' ORDER BY updated DESC", (chat,)).fetchall()
    out = [(k, json.loads(raw)) for k, raw in rows]
    return [(k, d) for k, d in out if statuses is None or d.get("status") in statuses]


def read(root, chat, key):
    for k, d in dockets(root, chat):
        if k == key:
            return d
    raise ValueError("No such docket.")


def members_of(d):
    return list(dict.fromkeys(d.get("members", []) + d.get("unreviewed", [])))


def grouped(root, chat):
    """Every observation key that belongs to a docket not yet closed."""
    return {k for _, d in dockets(root, chat) if d.get("status") != "closed" for k in members_of(d)}


def text_of(d):
    return d.get("candidate_change") or d.get("title") or ""


def observed_at(root, chat, key):
    """When the observation was recorded: its `created` field, else the time of its first version."""
    with closing(_connect(root)) as db:
        rec = db.execute("SELECT data,updated FROM records WHERE chat=? AND kind='lesson' AND key=?", (chat, key)).fetchone()
        if not rec:
            return 0
        created = json.loads(rec["data"]).get("created")
        if created:
            return created
        first = db.execute("SELECT min(updated) FROM history WHERE chat=? AND kind='lesson' AND key=?", (chat, key)).fetchone()[0]
    return first or rec["updated"]


def _similar(a, b):
    g, h = learning._grams(a), learning._grams(b)
    return bool(g and h) and len(g & h) / len(g | h) >= SIMILARITY


def propose_groups(root, chat, now=None):
    """The runtime's proposal of membership, from the similarity measure, over observations nobody has judged yet
    (no review state): one that resembles a docket's first member attaches to it as unreviewed; the rest that resemble
    each other become a new proposed docket. Nothing here is membership until the owning agent confirms it (group).
    Returns the docket keys touched."""
    now = now or time.time()
    lessons = dict(learning._lessons(root, chat, ("candidate",)))
    loose = [(k, d) for k, d in lessons.items() if not d.get("review")]
    touched = []
    for key, docket in dockets(root, chat):
        if docket.get("status") == "closed" or not members_of(docket):
            continue
        head = text_of(learning.read(root, chat, members_of(docket)[0]))
        added = [k for k, d in loose if _similar(text_of(d), head)]
        if added:
            docket["unreviewed"] = list(dict.fromkeys(docket.get("unreviewed", []) + added))
            _save(root, chat, key, docket)
            for k in added:
                _mark(root, chat, k, key, "proposed", at=now)
            touched.append(key)
            loose = [(k, d) for k, d in loose if k not in added]
    groups = []
    for key, d in loose:
        for group in groups:
            if _similar(text_of(d), text_of(group[0][1])):
                group[1].append(key)
                break
        else:
            groups.append(((key, d), [key]))
    for (first, d), keys in groups:
        if len(keys) < 2:
            continue
        key = _docket_key(first)
        method = next((((lessons[k].get("proposal") or {}).get("method_path")) for k in keys if (lessons[k].get("proposal") or {}).get("method_path")), "")
        _save(root, chat, key, dict(title=text_of(d)[:160], method_path=method, cause="", members=[], unreviewed=keys,
                                    status="proposed", proposed_by="similarity", created=now, notes=[]))
        for k in keys:
            _mark(root, chat, k, key, "proposed", at=now)
        touched.append(key)
    return touched


def group(root, chat, docket_key, members, method_path="", cause=""):
    """The owning agent confirms membership: the keys become reviewed members of the docket (created when docket_key is
    empty). Members must be open observations of this agent not held by another docket. The method file, when given,
    must be one of this agent's; the cause claim is the docket's, refined by the proposal that closes it."""
    members = list(dict.fromkeys(members or []))
    if not members:
        raise ValueError("Name the observations to group.")
    key = docket_key or _docket_key(members[0])
    existing = dict(dockets(root, chat)).get(key)
    if docket_key and existing is None:
        raise ValueError("No such docket.")
    docket = existing or dict(title="", method_path="", cause="", members=[], unreviewed=[], status="proposed",
                             proposed_by="agent", created=time.time(), notes=[])
    if docket.get("status") == "closed":
        raise ValueError("The docket is closed; start a new one from the observations.")
    held = {k: t for t, d in dockets(root, chat) if t != key and d.get("status") != "closed" for k in members_of(d)}
    for k in members:
        if learning.read(root, chat, k).get("status") not in learning.OPEN:
            raise ValueError(f"{k} is not an open observation.")
        if k in held:
            raise ValueError(f"{k} belongs to docket {held[k]}; ungroup it there first.")
    if method_path:
        docket["method_path"] = str(learning._skill_file(root, method_path))
    if cause:
        docket["cause"] = " ".join(str(cause).split())
    docket["members"] = list(dict.fromkeys(docket.get("members", []) + members))
    docket["unreviewed"] = [k for k in docket.get("unreviewed", []) if k not in docket["members"]]
    docket["title"] = docket.get("title") or text_of(learning.read(root, chat, members[0]))[:160]
    if docket["status"] == "proposed":
        docket["status"] = "open"
    _save(root, chat, key, docket)
    for k in members:
        _mark(root, chat, k, key, "member")
    return dict(docket=key, status=docket["status"], members=len(docket["members"]), unreviewed=len(docket["unreviewed"]))


def ungroup(root, chat, docket_key, members, note=""):
    """Membership is reversible: the keys leave the docket and are open observations again, with their own records and
    times untouched except for the verdict, which says they were judged and keeps the runtime from proposing them
    again. An emptied docket closes."""
    docket = read(root, chat, docket_key)
    members = list(dict.fromkeys(members or []))
    if not members:
        raise ValueError("Name the observations to ungroup.")
    unknown = [k for k in members if k not in members_of(docket)]
    if unknown:
        raise ValueError("Not in this docket: " + ", ".join(unknown))
    docket["members"] = [k for k in docket.get("members", []) if k not in members]
    docket["unreviewed"] = [k for k in docket.get("unreviewed", []) if k not in members]
    docket.setdefault("notes", []).append(dict(at=time.time(), ungrouped=members, note=" ".join(str(note or "").split())))
    if not members_of(docket):
        docket["status"] = "closed"
        docket["closed"] = dict(at=time.time(), reason=note or "every observation ungrouped")
    _save(root, chat, docket_key, docket)
    for k in members:
        _mark(root, chat, k, docket_key, "excluded", note)
    return dict(docket=docket_key, status=docket["status"], members=len(docket["members"]), unreviewed=len(docket["unreviewed"]))


def close_for(root, chat, lesson_key, explains):
    """An applied proposal closes the dockets its explained observations belong to."""
    closed = []
    for key, docket in dockets(root, chat):
        if docket.get("status") != "closed" and set(members_of(docket)) & set(explains or []):
            docket["status"] = "closed"
            docket["closed"] = dict(at=time.time(), by=lesson_key)
            _save(root, chat, key, docket)
            closed.append(key)
    return closed


def recent_count(root, chat, docket, now=None):
    now = now or time.time()
    return sum(1 for k in members_of(docket) if now - observed_at(root, chat, k) <= WINDOW_DAYS * 86400)


def _proposed_since(root, chat, docket, since):
    """Observations proposed into the docket after `since` and not yet ruled on: the only new evidence a review can add."""
    count = 0
    for k in docket.get("unreviewed", []):
        state = learning.read(root, chat, k).get("review") or {}
        if state.get("verdict") == "proposed" and state.get("at", 0) > since:
            count += 1
    return count


def due(root, chat, store, now=None):
    """Dockets that cross the threshold and have no review in flight: a first review, or another round once THRESHOLD
    observations have been proposed into the docket since its last review started."""
    out = []
    for key, docket in dockets(root, chat):
        if docket.get("status") not in ("proposed", "open", "review") or recent_count(root, chat, docket, now) < THRESHOLD:
            continue
        reviews = docket.get("reviews") or []
        if reviews:
            try:
                settled = store.get(reviews[-1]["task"])["status"] not in ACTIVE
            except Exception:
                settled = True
            if not settled or _proposed_since(root, chat, docket, reviews[-1]["at"]) < THRESHOLD:
                continue
        out.append((key, docket))
    return out


def start_review(root, chat, store, key, docket, now=None):
    """One task in the runtime lane for one docket: the review is the agent's next idle work, never a message to the
    owner. Keyed by the docket, so the same review is never queued twice."""
    now = now or time.time()
    items = []
    for k in members_of(docket):
        d = learning.read(root, chat, k)
        items.append(f"- {k}; {time.strftime('%Y-%m-%d', time.localtime(observed_at(root, chat, k)))}; {text_of(d)[:300]}")
    request = BRIEF.format(prefix=PREFIX, docket=key, n=len(items), window=WINDOW_DAYS,
                           method=docket.get("method_path") or "not yet named: name it with group(method_path=...)", items="\n".join(items))
    row = store.start(chat, request, "", extra={"notify_telegram": False, "docket": key})
    docket["review_task"] = row["id"]
    docket["status"] = "review"
    docket.setdefault("reviews", []).append(dict(task=row["id"], at=now, observations=len(items)))
    _save(root, chat, key, docket)
    return row["id"]


def flush(agent, now=None):
    """Called every minute by the engine's worker loop for roles that learn: propose membership for new observations,
    then start the reviews that are due. Returns the task ids started."""
    if not getattr(agent._background.role, "learns", False):
        return []
    root = agent.AGENT_DIR
    with closing(_connect(root)) as db:
        chats = [r[0] for r in db.execute("SELECT DISTINCT chat FROM records WHERE kind='lesson'")]
    started = []
    for chat in chats:
        propose_groups(root, chat, now)
        for key, docket in due(root, chat, agent._background, now):
            started.append(start_review(root, chat, agent._background, key, docket, now))
    return started


def reviews(root, chat, store=None):
    """The dashboard's method reviews: every docket not closed, newest first, with its counts and the review's state."""
    out = []
    for key, docket in dockets(root, chat):
        if docket.get("status") == "closed":
            continue
        task = docket.get("review_task")
        state = ""
        if task and store is not None:
            try:
                state = store.get(task)["status"]
            except Exception:
                state = "unknown"
        out.append(dict(key=key, title=docket.get("title", ""), method=Path(docket["method_path"]).name if docket.get("method_path") else "",
                        cause=docket.get("cause", ""), status=docket.get("status"), members=len(docket.get("members", [])),
                        unreviewed=len(docket.get("unreviewed", [])), recent=recent_count(root, chat, docket),
                        review_task=task, review_state=state, updated=docket.get("updated", 0)))
    return out


def backfill_review_state(root, chat):
    """Observations grouped, proposed or ungrouped before review states existed get theirs from the docket records
    (members: member; unreviewed: proposed at the docket's creation; ungrouped in a note: excluded, which wins and leaves
    the unreviewed list). Returns the number of observations marked."""
    marked = 0
    verdicts = {}
    for key, docket in dockets(root, chat):
        for k in docket.get("members", []):
            verdicts[k] = (key, "member", docket.get("created") or docket.get("updated", 0), "")
        for k in docket.get("unreviewed", []):
            verdicts.setdefault(k, (key, "proposed", docket.get("created") or docket.get("updated", 0), ""))
    for key, docket in dockets(root, chat):
        for note in docket.get("notes", []):
            for k in note.get("ungrouped", []):
                verdicts[k] = (key, "excluded", note.get("at", 0), note.get("note", ""))
        cleaned = [k for k in docket.get("unreviewed", []) if verdicts.get(k, ("", ""))[1] != "excluded"]
        if cleaned != docket.get("unreviewed", []):
            docket["unreviewed"] = cleaned
            _save(root, chat, key, docket)
    for k, (docket_key, verdict, at, note) in verdicts.items():
        try:
            if learning.read(root, chat, k).get("review"):
                continue
        except ValueError:
            continue
        _mark(root, chat, k, docket_key, verdict, note, at)
        marked += 1
    return marked


def migrate(root, chat):
    """Migration (design section 6): existing checks carry no receipts and become judgements; counts are recomputed
    from measured checks; review states are backfilled from the docket records; the grouping is run once and shown as
    proposed membership. Safe to run twice."""
    relabelled = 0
    for key, d in learning._lessons(root, chat, ("applied", "verified", "candidate", "proposed", "retired", "expired", "rejected")):
        checks = learning.checks_of(d)
        if checks == d.get("checks") and not any(not c.get("judgement") and "task" not in c for c in checks):
            continue

        def change(x):
            x["checks"] = learning.checks_of(x)
            for c in x["checks"]:
                if "task" not in c:
                    c["judgement"] = True
            measured = [c for c in x["checks"] if not c.get("judgement")]
            x["helped"] = sum(1 for c in measured if c.get("worked"))
            x["hurt"] = sum(1 for c in measured if not c.get("worked"))
        learning._update(root, chat, key, change)
        relabelled += 1
    marked = backfill_review_state(root, chat)
    return dict(relabelled=relabelled, marked=marked, dockets=propose_groups(root, chat))
