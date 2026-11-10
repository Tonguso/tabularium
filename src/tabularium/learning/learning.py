"""Lessons: one flow, Alex's (2026-10-12); a lesson names its cause (2026-11-10).

    record  ->  show the proposed improvement  ->  apply it after his go  ->  check whether it works
    candidate   proposed                          applied                    verified (or back to candidate)

Before this there were three paths that disagreed: approve a lesson and then separately approve a skill edit
(approve / mark_folded), an operator-only deploy that injected "validated guidance" beside the skill (deploy /
guidance, never used), and a weekly script that batched candidates into grouped changes (learning_loop.py). Now the
thing Alex approves is the exact edit, the runtime applies exactly that edit, and the lesson stays open until a real
task shows whether the failure recurred. A model cannot move a lesson past 'candidate' through project_record; only
these functions change its status, and approval needs Alex's own words from the conversation record.

2026-10-24, after Cato's survey of learning frameworks (ACE's delta edits): an approved lesson that adds a rule is
appended as one entry in the skill's "## Lessons" ledger, tagged with its key, never as a rewrite of the skill's
prose. Every check on a later task counts for the entry (helped / hurt), and a verified entry keeps being counted.
The weekly digest names entries that hurt more than they helped and entries that look alike, for Alex to retire or
merge; retire removes the entry on his words. Nothing here grades a lesson from the agent's opinion: counts come from
recorded checks, each with evidence.

2026-11-10, "Lessons That Change Methods" (docs/design): a month of lessons had produced nineteen ledger reminders and no
rewritten rule, while one failure recurred 116 times as unread candidates. A failure is an observation; a lesson is a
claim about a cause, anchored in the method (a passage quoted from it, or the statement that it is silent), with the
observations it explains as evidence. The edit rewrites the anchored rule; appending is allowed only where the method
is silent, and never past LEDGER_CAP entries. A check counts only when it is bound to a task, a receipt in the records
and the method version; otherwise it is a judgement, kept and shown, counting nothing. "apply it" on a skill without
usable cases applies nothing: the owner chooses no trial or a case first. Fold absorbs proven entries into prose.
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from tabularium.records.project_store import ProjectStore

OPEN = ("candidate", "proposed", "applied")
LEDGER = "## Lessons"
LEDGER_NOTE = ("Entries added by approved lessons (tabularium.learning.learning). Each is checked on later tasks and counted; "
               "retired entries are removed. Edit this section only through the lesson tool.")
LEDGER_CAP = 5           # net additions: a ledger never grows past this; rewrites, removals and folds are always allowed
RECEIPT = re.compile(r'^(task|verification|message):(\d+)$')
TASK_SOURCE = re.compile(r'[\\/]background[\\/](\d+)[\\/]')


def checks_of(d):
    """A lesson's checks as records. Early lessons (model-written, October 2026) hold `checks` as one string or as
    bare strings; each is read as a judgement with that text as its evidence and no verdict."""
    raw = d.get('checks')
    items = raw if isinstance(raw, list) else ([raw] if raw else [])
    return [c if isinstance(c, dict) else {'worked': None, 'evidence': str(c), 'judgement': True, 'at': d.get('_updated', 0)} for c in items]


def ledger_entries(text):
    return [l for l in text.splitlines() if l.startswith("- [lesson-")]


def ledger_entry(key, text, at=None):
    return f"- [{key}] ({time.strftime('%Y-%m-%d', time.localtime(at or time.time()))}) " + " ".join(text.split())


def add_entry(text, key, new_text, at=None):
    """Append one ledger entry at the end of the skill, creating the ledger section once."""
    entry = ledger_entry(key, new_text, at)
    body = text.rstrip("\n")
    if LEDGER not in body:
        body += f"\n\n{LEDGER}\n{LEDGER_NOTE}\n"
    return body + "\n" + entry + "\n"


def remove_entry(text, key):
    """Drop the ledger line tagged with `key`; True when a line was removed."""
    lines = text.splitlines(keepends=True)
    kept = [l for l in lines if not l.startswith(f"- [{key}]")]
    return "".join(kept), len(kept) != len(lines)


def propose(root, chat, content, source, task_type='other'):
    """Record a lesson: an observed failure and the correction it suggests. Status 'candidate'."""
    content = ' '.join(content.split())
    if not content: raise ValueError('A lesson needs an observed failure and proposed correction.')
    key = 'lesson-' + hashlib.sha256((str(source) + '\n' + content).encode()).hexdigest()[:16]
    store = ProjectStore(root)
    previous = store.manage(chat, 'read', 'lesson', key)
    if 'error' not in previous: return key
    store.manage(chat, 'save', 'lesson', key, dict(title=content[:160], failure=content, source=source,
        candidate_change=content, checks=[], limitations='Candidate: show Alex the proposed skill edit before anything changes.',
        status='candidate', task_types=[task_type], created=time.time()))
    from tabularium.learning import lesson_cases   # every lesson carries its own benchmark case (2026-10-26)
    lesson_cases.draft(root, chat, key)
    return key


def owner_said(root, chat, source_id, quote):
    """Alex's own words: message id + exact quote, checked against the conversation record, or ValueError."""
    recall = Path(root).resolve() / 'task_state/conversation_recall.sqlite3'
    with closing(sqlite3.connect(recall.as_uri() + '?mode=ro', uri=True)) as db:
        row = db.execute('SELECT speaker,text FROM messages WHERE id=? AND chat=?', (int(source_id), str(chat))).fetchone()
    if not (quote or '').strip() or not row or row[0] != 'user' or quote not in row[1]:
        raise ValueError('Exact user approval quote not found.')


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(root, chat, key):
    """One lesson's data, or ValueError."""
    with closing(ProjectStore(Path(root).resolve()).connect()) as db:
        rec = db.execute("SELECT data FROM records WHERE chat=? AND kind='lesson' AND key=?", (chat, key)).fetchone()
    if not rec: raise ValueError('No such lesson.')
    return json.loads(rec['data'])


def set_trial(root, chat, key, record):
    """The trial record on a proposed lesson (tabularium.learning.trials writes it; the model cannot)."""
    def change(d):
        if d.get('status') != 'proposed': raise ValueError('Only a proposed lesson has a trial.')
        d['trial'] = record
    _update(root, chat, key, change)


def _update(root, chat, key, change):
    """Read-modify-write one lesson under a write lock, keeping the previous version in history."""
    store = ProjectStore(Path(root).resolve())
    with closing(store.connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        rec = db.execute("SELECT * FROM records WHERE chat=? AND kind='lesson' AND key=?", (chat, key)).fetchone()
        if not rec: raise ValueError('No such lesson.')
        d = json.loads(rec['data'])
        result = change(d)
        db.execute('INSERT INTO history VALUES(?,?,?,?,?,?)', tuple(rec))
        db.execute('INSERT OR REPLACE INTO records VALUES(?,?,?,?,?,?)',
                   (chat, 'lesson', key, rec['version'] + 1, json.dumps(d, ensure_ascii=False), time.time()))
    return result


def still_applies(method, p):
    """The edit still applies as Alex saw it: the passage it replaces appears exactly once. Other changes to the same
    file, sibling edits and version bumps among them, do not invalidate it (2026-11-08: ten approved edits had failed
    a whole-file hash check because other approved edits landed first)."""
    if p.get('current_text') and Path(method).read_text(encoding='utf-8').count(p['current_text']) != 1:
        raise ValueError('The passage this edit replaces is no longer in the skill as proposed; propose it again.')


def _skill_file(root, method_path):
    root = Path(root).resolve()
    method = Path(method_path)
    method = (method if method.is_absolute() else root / method).resolve()
    if not (method == root / 'SKILL.md' or method.is_relative_to(root / 'skills')) or not method.is_file():
        raise ValueError('method_path must be an existing file under this agent\'s skills folder, or its SKILL.md.')
    return method


def check_cause(method, text, cause, current_text):
    """The decision in the method that allowed the failure: a claim with an anchor, a passage quoted from the method
    (present once, and the edit rewrites it), or the statement that the method is silent at a named place (then the
    edit may append). The runtime checks the anchor; the claim is for the owner to judge against the instances."""
    cause = dict(cause or {})
    claim = ' '.join(str(cause.get('claim') or '').split())
    anchor, silent = str(cause.get('anchor') or ''), ' '.join(str(cause.get('silent_at') or '').split())
    if not claim:
        raise ValueError('A proposal names its cause: the decision in the method that allowed the failure (cause.claim).')
    if anchor:
        if text.count(anchor) != 1:
            raise ValueError('cause.anchor must be quoted exactly from the method file and occur once.')
        if anchor not in (current_text or ''):
            raise ValueError('The edit rewrites the rule the cause anchors: current_text must contain the anchor.')
    elif not silent:
        raise ValueError('cause needs an anchor quoted from the method, or silent_at: where the method says nothing.')
    return {'claim': claim, 'anchor': anchor, 'silent_at': silent, 'file_sha256': _sha(method)}


def has_receipt(root, chat, d):
    """An observation with a receipt: its source is the owner's message in the conversation record, or a task
    whose folder is in this agent's records."""
    root = Path(root).resolve()
    source = str(d.get('source') or '')
    m = re.match(r'message:(\d+)$', source)
    if m:
        recall = root / 'task_state/conversation_recall.sqlite3'
        if not recall.exists(): return False
        with closing(sqlite3.connect(recall.as_uri() + '?mode=ro', uri=True)) as db:
            return bool(db.execute("SELECT 1 FROM messages WHERE id=? AND chat=? AND speaker='user'", (int(m.group(1)), str(chat))).fetchone())
    m = TASK_SOURCE.search(source)
    return bool(m and (root / 'task_state' / 'background' / m.group(1)).is_dir())


def check_explains(root, chat, key, method, explains):
    """The observations this cause accounts for: open lessons of this agent about the same method, or about no method
    yet. The proposing lesson is one of them. One instance is enough when it carries a receipt; a claim with neither
    receipt nor a second instance is an observation and stays a candidate."""
    keys = []
    for k in [key, *(explains or [])]:
        if k in keys: continue
        d = read(root, chat, k)
        if k != key and d.get('status') not in OPEN:
            raise ValueError(f'{k} is not an open observation.')
        other = (d.get('proposal') or {}).get('method_path')
        if other and Path(other).resolve() != method:
            raise ValueError(f'{k} is about {Path(other).name}, not {method.name}.')
        keys.append(k)
    if len(keys) == 1 and not has_receipt(root, chat, read(root, chat, key)):
        raise ValueError('One instance without a receipt is an observation, not a cause: name the other observations '
                         'this cause explains (explains), or record it from the task or message that showed it.')
    return keys


def propose_change(root, chat, key, method_path, current_text, new_text, cause=None, explains=None):
    """Step 2: the exact edit Alex will see, with its cause (check_cause) and the observations it explains
    (check_explains). current_text must occur exactly once in the skill file; empty appends new_text as a ledger
    entry, allowed only where the cause says the method is silent and while the ledger is under LEDGER_CAP. The
    file's hash is kept, so the edit applied later is the edit he saw."""
    method = _skill_file(root, method_path)
    text = method.read_text(encoding='utf-8')
    if not (new_text or '').strip(): raise ValueError('new_text is empty.')
    if current_text and text.count(current_text) != 1:
        raise ValueError('current_text must appear exactly once in the skill file; quote it exactly.')
    cause = check_cause(method, text, cause, current_text)
    explained = check_explains(root, chat, key, method, explains)
    if not current_text and len(ledger_entries(text)) >= LEDGER_CAP:
        raise ValueError(f'The ledger of {method.name} holds {LEDGER_CAP} entries: rewrite the rule the cause anchors, '
                         'or fold proven entries into the prose first.')

    def change(d):
        if d.get('status') not in ('candidate', 'proposed'):
            raise ValueError('Only a candidate or a pending proposal can be (re)proposed.')
        if d.get('trial', {}).get('version') == 2 and not d['trial'].get('notified'):
            raise ValueError('A trial is running; wait for its comparison before changing the proposal.')
        if d.get('trial'):
            d.setdefault('trial_history', []).append(d.pop('trial'))
        d['status'] = 'proposed'
        d['proposal'] = {'method_path': str(method), 'current_text': current_text or '', 'new_text': new_text,
                         'file_sha256': _sha(method), 'at': time.time()}
        d['cause'] = cause; d['explains'] = explained
        d.pop('go_blocked', None)
        case = d.get('case') or {}
        if case.get('status') == 'draft' and not case.get('requirements'):
            case['rule'] = ' '.join(new_text.split())     # the case states the rule as the skill will carry it (2026-11-02)
    _update(root, chat, key, change)
    shown = (f"Proposed change to {method.name} ({method.parent.name}):\nCAUSE: {cause['claim']}\n"
             + (f"ANCHOR: {cause['anchor']}\n" if cause['anchor'] else f"THE METHOD IS SILENT AT: {cause['silent_at']}\n")
             + f"EXPLAINS: {', '.join(explained)}\n"
             + (f"REPLACE:\n{current_text}\nWITH:\n{new_text}" if current_text else f"ADD AT THE END:\n{new_text}"))
    return {'key': key, 'status': 'proposed', 'show_owner': shown}


def propose_fold(root, chat, method_path, current_text, new_text, keys, source='fold'):
    """Fold: ledger entries with measured checks absorbed into the skill's prose. One proposal, approved like any
    edit, applied in one write guarded by the file's hash; the entries' keys stay on the record as the provenance of
    the prose. An entry without a measured check can be retired, not folded."""
    method = _skill_file(root, method_path)
    text = method.read_text(encoding='utf-8')
    if not (current_text or '').strip() or text.count(current_text) != 1:
        raise ValueError('A fold rewrites prose: current_text must occur exactly once in the skill file.')
    if not (new_text or '').strip(): raise ValueError('new_text is empty.')
    if not keys: raise ValueError('Name the ledger entries to fold.')
    for k in keys:
        d = read(root, chat, k)
        a = d.get('applied') or {}
        if d.get('status') not in ('applied', 'verified') or not a.get('entry') or Path(a.get('method_path', '')).resolve() != method:
            raise ValueError(f'{k} is not a ledger entry of {method.name}.')
        if not any(not c.get('judgement') for c in checks_of(d)):
            raise ValueError(f'{k} has no measured check; it can be retired, not folded.')
        if f'- [{k}]' not in text:
            raise ValueError(f'{k} is not in the ledger of {method.name}.')
    key = propose(root, chat, f'Fold {len(keys)} ledger entries of {method.name} into its prose: ' + ' '.join(new_text.split())[:120], source)

    def change(d):
        d['status'] = 'proposed'
        d['proposal'] = {'method_path': str(method), 'current_text': current_text, 'new_text': new_text, 'fold': list(keys),
                         'file_sha256': _sha(method), 'at': time.time()}
    _update(root, chat, key, change)
    return {'key': key, 'status': 'proposed', 'show_owner': (f"Proposed fold in {method.name} ({method.parent.name}): "
            f"{len(keys)} ledger entries become prose.\nREPLACE:\n{current_text}\nWITH:\n{new_text}\nREMOVES ENTRIES: " + ', '.join(keys))}


def record(root, chat, content, source, method_path, current_text, new_text, task_type='other', cause=None, explains=None):
    """One step: the lesson and the exact skill edit it proposes, registered for Alex's go. A lesson without its edit
    is not recorded (2026-11-01: 183 candidates had been recorded, and of the last 41 only 6 ever got as far as a
    draft; Alex: [quote removed]). The edit is checked before anything is saved."""
    method = _skill_file(root, method_path)
    text = method.read_text(encoding='utf-8')
    if not (new_text or '').strip(): raise ValueError('A lesson is recorded with its edit: new_text is empty.')
    if current_text and text.count(current_text) != 1:
        raise ValueError('current_text must appear exactly once in the skill file; quote it exactly.')
    key = propose(root, chat, content, source, task_type)
    try:
        return propose_change(root, chat, key, method, current_text, new_text, cause, explains)
    except ValueError as exc:   # the observation stays recorded as a candidate; the claim was refused
        raise ValueError(f'{exc} The observation is recorded as {key} (a candidate); propose again with lesson action=propose.')


def proposals(root, chat):
    """Edits awaiting Alex's go, oldest first: what the dashboard shows and the weekly message carries."""
    out = []
    for key, d in _lessons(root, chat, ('proposed',)):
        p = d.get('proposal') or {}
        if not p: continue
        trial = d.get('trial') or {}
        if trial.get('version') == 2 and not trial.get('notified'): continue   # running on his go: the runtime's work, not his
        method = Path(p.get('method_path', ''))
        skills = Path(root).resolve() / 'skills'
        skill = method.relative_to(skills).as_posix() if method.is_relative_to(skills) else method.name
        if d.get('case_requested') and not trial.get('notified'): continue   # his choice: a case first; back with the trial
        c = d.get('cause') or {}
        shown = ((f"CAUSE: {c['claim']}\n" + (f"ANCHOR: {c['anchor']}\n" if c.get('anchor') else f"THE METHOD IS SILENT AT: {c.get('silent_at', '')}\n")
                  + f"EXPLAINS: {', '.join(d.get('explains') or [key])}\n") if c else '')
        shown += (f"REPLACE:\n{p['current_text']}\nWITH:\n{p['new_text']}" if p.get('current_text')
                  else f"ADD AT THE END:\n{p.get('new_text', '')}")
        if p.get('fold'): shown += '\nREMOVES ENTRIES: ' + ', '.join(p['fold'])
        if d.get('go_blocked'): shown += '\n\nNO TRIAL POSSIBLE: ' + d['go_blocked']['reason']
        if trial.get('notified') and trial.get('report'):
            try: shown += '\n\nTRIAL DONE:\n' + Path(trial['report']).read_text(encoding='utf-8')[:3000]
            except OSError: shown += '\n\nTRIAL DONE (report file missing).'
        out.append(dict(key=key, title=(d.get('title') or '')[:200], skill=skill, at=p.get('at') or d.get('_updated', 0), shown=shown,
                        blocked=bool(d.get('go_blocked')), fold=bool(p.get('fold'))))
    out.sort(key=lambda r: r['at'])
    return out


def awaiting_go(root, chat, limit=5):
    """The weekly message: the oldest `limit` proposed edits in full and how many more wait. Empty when none."""
    rows = proposals(root, chat)
    if not rows: return ''
    lines = [f"{len(rows)} skill edit(s) await your go. Say which keys to apply (a trial runs first unless you say no "
             "trial is needed) and which to drop."]
    for r in rows[:limit]:
        lines.append(f"\n{r['key']} ({r['skill']}): {r['title']}\n{r['shown']}")
    if len(rows) > limit:
        lines.append(f"\n...and {len(rows) - limit} more, all on the dashboard.")
    return '\n'.join(lines)


def _bump_version(text):
    def bump(m):
        parts = m.group(2).split('.')
        parts[-1] = str(int(parts[-1]) + 1) if parts[-1].isdigit() else parts[-1]
        return m.group(1) + '.'.join(parts) + m.group(3)
    head, sep, body = text.partition('\n---')
    return re.sub(r'(version:\s*"?)([0-9][0-9.]*)("?)', bump, head, count=1) + sep + body


def render_change(text, key, p):
    edited = text.replace(p['current_text'], p['new_text'], 1) if p['current_text'] else add_entry(text, key, p['new_text'], p['at'])
    for folded in p.get('fold', []):
        edited, _ = remove_entry(edited, folded)
    return _bump_version(edited)


def approve(root, chat, key, source_id, quote, skip_trial=False, reason=''):
    """Step 3: Alex's go, in his own words (message id + exact quote, checked against the conversation record),
    applies exactly the proposed edit and bumps the skill's patch version. Status 'applied'. A finished trial
    (tabularium.learning.trials) comes first, so he judges independent checks and actual outputs rather than the argument for the edit; skip_trial
    only when he said no trial is needed, and the lesson records that it was skipped (2026-10-20)."""
    root = Path(root).resolve()
    recall = root / 'task_state/conversation_recall.sqlite3'
    with closing(sqlite3.connect(recall.as_uri() + '?mode=ro', uri=True)) as db:
        row = db.execute('SELECT speaker,text FROM messages WHERE id=? AND chat=?', (int(source_id), str(chat))).fetchone()
    if not quote.strip() or not row or row[0] != 'user' or quote not in row[1]:
        raise ValueError('Exact user approval quote not found.')

    def change(d):
        if d.get('status') != 'proposed' or not d.get('proposal'):
            raise ValueError('Nothing to approve: show Alex the proposed edit first (action=propose).')
        from tabularium.learning.trials import evidence_matches
        matched = evidence_matches(d)
        if d.get('trial', {}).get('version') == 2 and not d['trial'].get('notified'):
            raise ValueError('Wait for the trial comparison before approving or skipping it.')
        if not matched and not skip_trial:
            raise ValueError('Run the trial first (lesson action=trial) so Alex sees both outputs, or pass skip_trial '
                             'only if he said no trial is needed.')
        if skip_trial and not matched:
            d['trial_skipped'] = {'quote': quote, 'at': time.time(), 'reason': reason}
        p = d['proposal']
        method = _skill_file(root, p['method_path'])
        still_applies(method, p)
        text = method.read_text(encoding='utf-8')
        if p.get('fold') and _sha(method) != p['file_sha256']:
            raise ValueError('The skill changed since the fold was proposed; propose it again from the current text.')
        if not p['current_text'] and len(ledger_entries(text)) >= LEDGER_CAP:
            raise ValueError(f'The ledger of {method.name} is at {LEDGER_CAP} entries; fold or retire before adding.')
        # A replacement sharpens an existing rule in place; an addition becomes one ledger entry (ACE, 2026-10-24).
        method.write_text(render_change(text, key, p), encoding='utf-8')
        d['status'] = 'applied'
        d['approval'] = {'source': f'message:{int(source_id)}', 'quote': quote, 'at': time.time()}
        d['applied'] = {'method_path': str(method), 'method_sha256': _sha(method), 'at': time.time(),
                        'entry': not p['current_text']}
        d.setdefault('helped', 0); d.setdefault('hurt', 0)
        return {'key': key, 'status': 'applied', 'skill': str(method)}
    out = _update(root, chat, key, change)
    applied = read(root, chat, key)
    for folded in (applied.get('proposal') or {}).get('fold', []):
        _update(root, chat, folded, lambda d: d.update(status='folded', folded_into=key, folded_at=time.time()))
    from tabularium.learning import themes
    themes.close_for(root, chat, key, applied.get('explains') or [key])
    return out


GO = re.compile(r"^Skill edit (lesson-\w+): (apply it, no trial needed|apply it|drop it|draft a case first)\.?\s*$")


def settle_go(root, chat, key, verdict, source_id, quote, store, system_prompt=''):
    """Alex's verdict on a proposed edit, settled by the runtime the moment it arrives (2026-11-08: 25 edits he had
    said "apply it" to stood on his list for a day, because applying depended on the agent running a trial that no
    skill had cases for, then calling approve). drop: retired. apply, no trial: applied. apply: the trial runs
    when the skill has benchmark cases and the edit returns to his list with the report; when it has none, nothing
    is applied: the edit stays on his list with the reason and his two choices, no trial or a case first
    (2026-11-10: until then the exception handler applied it, so a missing case was a reason to apply). Returns one
    line for him; ValueError when nothing changed."""
    if verdict == 'drop it':
        out = retire(root, chat, key, source_id, quote)
        return f"Skill edit {key} retired. {out['note']}".strip()
    if verdict == 'apply it, no trial needed':
        out = approve(root, chat, key, source_id, quote, skip_trial=True, reason='Alex: no trial needed')
        return f"Skill edit {key} applied to {Path(out['skill']).name}, no trial."
    if verdict == 'draft a case first':
        owner_said(root, chat, source_id, quote)

        def change(d):
            if d.get('status') != 'proposed': raise ValueError('Nothing to draft a case for: the edit is not pending.')
            d['case_requested'] = {'source': f'message:{int(source_id)}', 'quote': quote, 'at': time.time()}
            d.pop('go_blocked', None)
        _update(root, chat, key, change)
        return (f"Skill edit {key}: a case first. The agent fills and calibrates the lesson's case; the edit comes back "
                "to your list with the trial report. The skill is unchanged until then.")
    from tabularium.learning import trials
    if trials.evidence_matches(read(root, chat, key)):
        out = approve(root, chat, key, source_id, quote)
        return f"Skill edit {key} applied to {Path(out['skill']).name} after its trial."
    try:
        out = trials.start(root, chat, key, {}, store, system_prompt)
        return (f"Skill edit {key}: trial queued, {len(out['tasks'])} runs. It comes back to your list with the report; "
                "the skill is unchanged until then.")
    except ValueError as exc:
        reason = str(exc)
    owner_said(root, chat, source_id, quote)

    def change(d):
        if d.get('status') != 'proposed': raise ValueError('The edit is no longer pending.')
        d['go_blocked'] = {'reason': reason, 'source': f'message:{int(source_id)}', 'quote': quote, 'at': time.time()}
    _update(root, chat, key, change)
    return (f"Skill edit {key}: not applied. No trial possible: {reason} It stays on your list; say "
            "'apply it, no trial needed' or 'draft a case first'.")


def resolve_receipt(root, chat, task, receipt, method_sha):
    """True when a check is measured: the task's folder is in this agent's records, the receipt resolves there
    (task:<id> with a result, verification:<id> with a completion record, message:<id> from the owner) and the method
    version is a file hash. All three or none; none means a judgement."""
    if task is None and receipt is None and method_sha is None:
        return False
    if not (task and receipt and method_sha):
        raise ValueError('A measured check names task, receipt and method_sha together; leave all three out for a judgement.')
    root = Path(root).resolve()
    tasks = root / 'task_state' / 'background'
    if not (tasks / str(int(task))).is_dir():
        raise ValueError(f'No task {task} in the records.')
    m = RECEIPT.match(str(receipt))
    if not m:
        raise ValueError('receipt is task:<id> (a result), verification:<id> (a completion record) or message:<id> (the owner).')
    kind, ident = m.group(1), int(m.group(2))
    if kind == 'task' and not (tasks / str(ident) / 'result.txt').exists():
        raise ValueError(f'Task {ident} has no result in the records.')
    if kind == 'verification' and not (tasks / str(ident) / 'completion.json').exists():
        raise ValueError(f'Task {ident} has no completion record.')
    if kind == 'message':
        recall = root / 'task_state/conversation_recall.sqlite3'
        with closing(sqlite3.connect(recall.as_uri() + '?mode=ro', uri=True)) as db:
            row = db.execute("SELECT 1 FROM messages WHERE id=? AND chat=? AND speaker='user'", (ident, str(chat))).fetchone()
        if not row:
            raise ValueError(f'No owner message {ident} in the conversation record.')
    if not re.fullmatch(r'[0-9a-f]{64}', str(method_sha)):
        raise ValueError('method_sha is the hash of the method file the task ran under (64 hex characters).')
    return True


def record_check(root, chat, key, worked, evidence, task=None, receipt=None, method_sha=None):
    """Step 4: after a real task where the failure could have recurred. A measured check (resolve_receipt) counts for
    the entry (helped or hurt): an applied lesson's first success makes it 'verified'; a failure before any success
    sends it back to 'candidate'; a verified entry keeps being counted and the digest offers it for retirement once it
    hurts more than it helps (ACE, 2026-10-24). One task counts once per lesson. A check without task, receipt and
    method version is a judgement: kept and shown with the lesson, counting nothing (2026-11-10)."""
    if not (evidence or '').strip(): raise ValueError('A check needs evidence: the task and what happened.')
    measured = resolve_receipt(root, chat, task, receipt, method_sha)

    def change(d):
        if d.get('status') not in ('applied', 'verified'): raise ValueError('Only an applied or verified lesson can be checked.')
        entry = {'worked': bool(worked), 'evidence': evidence, 'at': time.time()}
        if not measured:
            entry['judgement'] = True
            d['checks'] = checks_of(d) + [entry]
            return {'key': key, 'status': d['status'], 'helped': d.get('helped', 0), 'hurt': d.get('hurt', 0), 'judgement': True}
        if any(c.get('task') == int(task) for c in checks_of(d) if not c.get('judgement')):
            raise ValueError(f'Task {task} is already counted for this lesson.')
        entry.update(task=int(task), receipt=receipt, method_sha256=method_sha)
        d['checks'] = checks_of(d) + [entry]
        d['helped'] = d.get('helped', 0) + (1 if worked else 0)
        d['hurt'] = d.get('hurt', 0) + (0 if worked else 1)
        if worked:
            d['status'] = 'verified'
        elif d['status'] == 'applied':
            d['status'] = 'candidate'
            d.pop('proposal', None)
        return {'key': key, 'status': d['status'], 'helped': d['helped'], 'hurt': d['hurt']}
    return _update(root, chat, key, change)


def retire(root, chat, key, source_id, quote):
    """Remove a ledger entry on Alex's words (message id + exact quote, as for approve), or drop a proposed edit he
    has said no to (2026-11-02: the dashboard's "Drop"). A lesson that sharpened a rule in place has no entry to
    remove: it is marked retired and the digest says the rule needs a new proposal."""
    root = Path(root).resolve()
    recall = root / 'task_state/conversation_recall.sqlite3'
    with closing(sqlite3.connect(recall.as_uri() + '?mode=ro', uri=True)) as db:
        row = db.execute('SELECT speaker,text FROM messages WHERE id=? AND chat=?', (int(source_id), str(chat))).fetchone()
    if not quote.strip() or not row or row[0] != 'user' or quote not in row[1]:
        raise ValueError('Exact user retirement quote not found.')

    def change(d):
        if d.get('status') not in ('proposed', 'applied', 'verified'):
            raise ValueError('Only a proposed, applied or verified lesson can be retired.')
        dropped = d.get('status') == 'proposed'
        removed = False
        applied = d.get('applied') or {}
        if applied.get('method_path'):
            method = _skill_file(root, applied['method_path'])
            text, removed = remove_entry(method.read_text(encoding='utf-8'), key)
            if removed:
                method.write_text(_bump_version(text), encoding='utf-8')
        d['status'] = 'retired'
        d['retired'] = {'source': f'message:{int(source_id)}', 'quote': quote, 'at': time.time(), 'entry_removed': removed}
        return {'key': key, 'status': 'retired', 'entry_removed': removed,
                'note': ('Dropped before it was applied; the skill is unchanged.' if dropped else '' if removed else
                         'This lesson changed a rule in place; the rule stays until a new proposal replaces it.')}
    return _update(root, chat, key, change)


def _lessons(root, chat, statuses):
    path = Path(root) / 'task_state/projects.sqlite3'
    if not path.exists(): return []
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='records'").fetchone(): return []
        rows = db.execute("SELECT key,data,updated FROM records WHERE chat=? AND kind='lesson' ORDER BY updated DESC", (chat,)).fetchall()
    out = []
    for key, raw, updated in rows:
        d = json.loads(raw); d['_updated'] = updated
        if d.get('status') in statuses: out.append((key, d))
    return out


TAGS = {'applied': 'APPLIED to the skill: on this task, check whether the failure recurred, then record it (lesson action=checked)',
        'proposed': 'edit proposed, awaiting Alex\'s go',
        'candidate': 'candidate, unapproved: use judgment'}


def hints(root, chat, task_type='', limit=6):
    """Open lessons for the next task, labelled. Applied ones first: this task is where they get checked."""
    rows = _lessons(root, chat, OPEN)
    rows = [r for r in rows if not task_type or not r[1].get('task_types') or task_type in r[1].get('task_types', [])
            or 'other' in r[1].get('task_types', [])]
    order = {'applied': 0, 'proposed': 1, 'candidate': 2}
    rows.sort(key=lambda r: (order[r[1]['status']], 0 if task_type and task_type in r[1].get('task_types', []) else 1, -r[1].get('_updated', 0)))
    def tag(d):
        if d['status'] == 'proposed' and d.get('case_requested'):
            return "Alex wants a case before the trial: fill the lesson's case (lesson action=case), calibrate it, then run the trial"
        return TAGS[d['status']]
    return '\n'.join(f"- [{key}] ({tag(d)}) {d.get('candidate_change') or d.get('title')}" for key, d in rows[:limit])


def _grams(text):
    t = ' '.join(str(text).lower().split())
    return {t[i:i + 2] for i in range(len(t) - 1)} if len(t) > 1 else set()


def similar(root, chat, content, threshold=0.3):
    """Key of an open lesson this content restates (character-bigram Jaccard: a paraphrase scores ~0.75, the same theme
    ~0.33, unrelated lessons ~0.15-0.2), else ''. Information for the agent and for Alex; it authorizes nothing."""
    g = _grams(content)
    if not g: return ''
    best = ('', 0.0)
    for key, d in _lessons(root, chat, OPEN):
        h = _grams(d.get('candidate_change') or d.get('title') or '')
        if not h: continue
        score = len(g & h) / len(g | h)
        if score >= threshold and score > best[1]: best = (key, score)
    return best[0]


TOOL = {"name":"lesson","description":"Turn an observed failure into a change of method, Alex's way: name the cause, show him the edit, apply it on his go, then measure whether it worked. A failure is an observation (recorded by remember kind=lesson, or by a task checkpoint); a lesson is a claim about the decision in a method that allowed it. propose: (re)draft the edit of a recorded lesson: method_path the skill file; cause {claim: the decision that allowed the failure, in one or two sentences; anchor: the passage quoted exactly from the method, which current_text must then contain, because the edit rewrites that rule; or silent_at: where the method says nothing, the only case in which current_text may be empty to add one entry to the skill's '## Lessons' ledger (never past five entries)}; explains: keys of the other open observations this cause accounts for (one instance is enough only when it comes from a task record or Alex's message); new_text in the skill's own voice. The runtime checks the anchor and the observations, shows every proposal to Alex on the dashboard and in a weekly message, and settles his verdict ('Skill edit <key>: apply it' / 'apply it, no trial needed' / 'drop it' / 'draft a case first') before any turn; you see the outcome in the conversation and do nothing for it. fold: ledger entries with measured checks (keys) absorbed into the prose: current_text the prose to rewrite, new_text the prose that now carries them; on his go the runtime rewrites and removes the entries in one write. trial: compare frozen skills on reviewed benchmark cases using disjoint development_cases and heldout_cases IDs, repetitions and max_steps; exposed cases cannot be reused as held-out evidence; small samples are screening, never proof. approve: only after Alex says yes in his own words (message id and exact quote), normally after the trial; skip_trial=true only if he said no trial is needed; the runtime applies exactly the proposed edit and bumps the skill's version; you never edit the file yourself for a lesson. checked: after the next real task where the failure could have recurred: worked, evidence, and for a measured check the task id, a receipt (task:<id>, verification:<id> or message:<id>) and method_sha (the hash of the method file the task ran under); without those three the check is a judgement, kept but counting nothing; a measured recurrence reopens the lesson. group / ungroup: membership of a theme (theme key, members: observation keys; group also takes method_path and cause): the runtime proposes membership from similarity and opens a method review at three observations in sixty days; only you confirm it, and ungroup reverses it without loss. retire: drop a proposed edit, or remove an applied ledger entry, on Alex's quoted words. case: fill in the lesson's drafted benchmark case (oracle_basis, requirements with id, description and a check of kind json/text/csv/xlsx/judgment, and fixtures: input files, a scripted delegate result, a scripted verification result or scripted search results, so a method about delegating, verifying or reading inputs can be exercised) so code can grade it; calibrate: isolated runs per arm, after which the runtime refuses a case that mostly fails even with the rule stated or mostly passes without it; approve_case: Alex's quoted yes adds a calibrated case to the benchmark; recombine: draft a reserved (held-out) case from this and one or two other approved cases of the same skill.",
  "input_schema":{"type":"object","properties":{"action":{"type":"string","enum":["propose","fold","group","ungroup","trial","approve","checked","retire","case","calibrate","approve_case","recombine"]},"slot":{"type":"string","enum":["case","reserved"]},"request":{"type":"string","description":"case: the task as Alex would ask it (defaults to the lesson's task request)"},"oracle_basis":{"type":"string","description":"case: how the expected answer was established, independent of the agent"},"requirements":{"type":"array","items":{"type":"object"},"description":"case: benchmark requirements, each with id, description and a check"},"fixtures":{"type":"object","description":"case: what the trial run needs, provided without side effects: files [{path, content}] placed in the run's inputs folder; delegate {result} returned by a scripted delegate; verify {exit_code, output} returned by execute_command; search [{title, url, snippet}] returned by web_search"},"with_keys":{"type":"array","items":{"type":"string"},"description":"recombine: one or two other lesson keys of the same skill"},"key":{"type":"string"},"cause":{"type":"object","properties":{"claim":{"type":"string"},"anchor":{"type":"string"},"silent_at":{"type":"string"}},"description":"propose: the decision in the method that allowed the failure, anchored in the method (anchor quoted exactly) or silent_at; group: the same object, its claim kept on the theme"},"explains":{"type":"array","items":{"type":"string"},"description":"propose: keys of the other open observations this cause accounts for"},"keys":{"type":"array","items":{"type":"string"},"description":"fold: the ledger entries to absorb"},"theme":{"type":"string","description":"group, ungroup: the theme key (group: empty starts a new theme)"},"members":{"type":"array","items":{"type":"string"},"description":"group, ungroup: observation keys"},"note":{"type":"string","description":"ungroup: why"},"development_cases":{"type":"array","items":{"type":"string"}},"heldout_cases":{"type":"array","items":{"type":"string"}},"repetitions":{"type":"integer","minimum":1,"maximum":5},"max_steps":{"type":"integer","minimum":1,"maximum":60},"skip_trial":{"type":"boolean","description":"approve: only when Alex said no trial is needed"},"method_path":{"type":"string","description":"propose, fold: the SKILL.md or references file under skills/"},"current_text":{"type":"string"},"new_text":{"type":"string"},"source_message_id":{"type":"integer"},"quote":{"type":"string"},"worked":{"type":"boolean"},"evidence":{"type":"string"},"task":{"type":"integer","description":"checked: the task where the failure could have recurred"},"receipt":{"type":"string","description":"checked: task:<id>, verification:<id> or message:<id>"},"method_sha":{"type":"string","description":"checked: the hash of the method file the task ran under"}},"required":["action"]}}


def execute(agent, chat, inputs):
    """The lesson tool for any agent whose role learns (Cato since 2026-10-10; Cal and Vit since 2026-10-20, when
    Alex's corrections started producing diagnosed lessons for them too)."""
    import json
    try:
        action = inputs.get("action")
        if action == "propose":
            return json.dumps(propose_change(agent.AGENT_DIR, chat, inputs["key"], inputs["method_path"],
                                             inputs.get("current_text", ""), inputs["new_text"],
                                             inputs.get("cause"), inputs.get("explains")), ensure_ascii=False)
        if action in ("group", "ungroup"):
            from tabularium.learning import themes
            if action == "group":
                return json.dumps(themes.group(agent.AGENT_DIR, chat, inputs.get("theme", ""), inputs.get("members") or [],
                                               inputs.get("method_path", ""), (inputs.get("cause") or {}).get("claim", "") if isinstance(inputs.get("cause"), dict) else inputs.get("cause", "")), ensure_ascii=False)
            return json.dumps(themes.ungroup(agent.AGENT_DIR, chat, inputs["theme"], inputs.get("members") or [], inputs.get("note", "")), ensure_ascii=False)
        if action == "fold":
            return json.dumps(propose_fold(agent.AGENT_DIR, chat, inputs["method_path"], inputs.get("current_text", ""),
                                           inputs["new_text"], inputs.get("keys") or []), ensure_ascii=False)
        if action == "trial":
            from tabularium.learning import trials
            return json.dumps(trials.start(agent.AGENT_DIR, chat, inputs["key"], inputs, agent._background, agent.SYSTEM_PROMPT),
                              ensure_ascii=False)
        if action == "approve":
            return json.dumps(approve(agent.AGENT_DIR, chat, inputs["key"], inputs["source_message_id"],
                                      inputs["quote"], skip_trial=bool(inputs.get("skip_trial"))), ensure_ascii=False)
        if action == "checked":
            out = record_check(agent.AGENT_DIR, chat, inputs["key"], inputs["worked"], inputs["evidence"],
                               inputs.get("task"), inputs.get("receipt"), inputs.get("method_sha"))
            if not inputs["worked"] and not out.get("judgement"):   # a measured recurrence is a failure the diagnosis should read (2026-10-24)
                from tabularium.learning import corrections
                corrections.start_failure(agent, chat, f"Lesson {inputs['key']} recurred", inputs["evidence"])
            return json.dumps(out, ensure_ascii=False)
        if action == "retire":
            return json.dumps(retire(agent.AGENT_DIR, chat, inputs["key"], inputs["source_message_id"], inputs["quote"]),
                              ensure_ascii=False)
        from tabularium.learning import lesson_cases
        if action == "case":
            return json.dumps(lesson_cases.fill(agent.AGENT_DIR, chat, inputs["key"], inputs.get("slot", "case"),
                                                inputs.get("request"), inputs.get("oracle_basis"), inputs.get("requirements"),
                                                inputs.get("fixtures")), ensure_ascii=False)
        if action == "calibrate":
            return json.dumps(lesson_cases.calibrate(agent.AGENT_DIR, chat, inputs["key"], agent._background, agent.SYSTEM_PROMPT,
                                                     inputs.get("slot", "case"), inputs.get("max_steps", 12)), ensure_ascii=False)
        if action == "approve_case":
            return json.dumps(lesson_cases.approve(agent.AGENT_DIR, chat, inputs["key"], inputs["source_message_id"], inputs["quote"],
                                                   inputs.get("slot", "case")), ensure_ascii=False)
        if action == "recombine":
            return json.dumps(lesson_cases.recombine(agent.AGENT_DIR, chat, inputs["key"], inputs.get("with_keys") or []), ensure_ascii=False)
        return "Use action propose, fold, group, ungroup, trial, approve, checked, retire, case, calibrate, approve_case or recombine."
    except (ValueError, KeyError, TypeError) as exc:
        return f"Not recorded: {exc}"


EXPIRE_DAYS = 30


def expire(root, chat, days=EXPIRE_DAYS, now=None):
    """Candidates untouched for `days` become 'expired': out of hints and out of the backlog, still in history.
    A repeat of an expired lesson creates a fresh candidate, which is the signal that it mattered (2026-10-20).
    An observation that belongs to a theme is evidence and never expires (2026-11-10)."""
    from tabularium.learning import themes
    now = now or time.time()
    gone = []
    held = themes.grouped(root, chat)
    for key, d in _lessons(root, chat, ('candidate',)):
        if key not in held and now - d.get('_updated', now) > days * 86400:
            def change(x):
                if x.get('status') == 'candidate':
                    x['status'] = 'expired'; x['expired_at'] = now
            _update(root, chat, key, change)
            gone.append(key)
    return gone


def digest(root, chat, limit=5):
    """The few open lessons worth Alex's eye: proposals awaiting him, then candidates grouped by theme with their
    repeat counts, most repeated first. Text for a weekly message."""
    rows = _lessons(root, chat, OPEN + ('verified',))
    proposed = [(k, d) for k, d in rows if d.get('status') == 'proposed']
    applied = [(k, d) for k, d in rows if d.get('status') == 'applied']
    cands = [(k, d) for k, d in rows if d.get('status') == 'candidate']
    entries = sorted((k, d) for k, d in rows if d.get('status') in ('applied', 'verified') and (d.get('applied') or {}).get('entry'))
    from tabularium.learning import themes as th   # stored themes (2026-11-10): membership the agent confirmed, plus proposals
    th.propose_groups(root, chat)
    stored = [(k, d) for k, d in th.themes(root, chat) if d.get('status') != 'closed']
    held = {m for _, d in stored for m in th.members_of(d)}
    groups = [(k, d.get('title') or '', th.members_of(d), d.get('status')) for k, d in stored]
    groups += [(k, d.get('candidate_change') or d.get('title') or '', [k], '') for k, d in cands if k not in held]
    groups.sort(key=lambda g: (-len(g[2]), g[0]))
    lines = [f"Lessons: {len(proposed)} proposed, {len(applied)} applied (awaiting a check), {len(cands)} candidates in {len(groups)} themes."]
    for key, d in proposed:
        lines.append(f"- PROPOSED {key}: {(d.get('title') or '')[:120]}  (awaiting your go; a trial runs first)")
    for key, title, members, status in groups[:limit]:
        label = {'review': '  (method review open)', 'open': '  (membership confirmed)', 'proposed': '  (membership proposed, unconfirmed)'}.get(status, '')
        lines.append(f"- x{len(members)} {key}: {title[:140]}{label}")
    if len(groups) > limit:
        lines.append(f"- and {len(groups) - limit} more themes; ungrouped observations expire after {EXPIRE_DAYS} days unless repeated.")
    from tabularium.learning import lesson_cases   # cases awaiting his yes, and drafts the agent still owes
    lines.extend(lesson_cases.digest_lines(rows))
    from tabularium.learning import regression
    gap = regression.route_gap(root)
    if gap:
        lines.append(gap)
    # Curation of the ledger (ACE): retire what hurts, merge what repeats. Proposals only; Alex decides.
    for key, d in entries:
        if d.get('hurt', 0) > d.get('helped', 0) and d.get('hurt', 0) >= 2:
            lines.append(f"- RETIRE? {key}: hurt {d['hurt']}, helped {d.get('helped', 0)}: {(d.get('title') or '')[:120]}")
    seen = set()
    for i, (k1, d1) in enumerate(entries):
        for k2, d2 in entries[i + 1:]:
            same_skill = (d1.get('applied') or {}).get('method_path') == (d2.get('applied') or {}).get('method_path')
            g, h = _grams(d1.get('title') or ''), _grams(d2.get('title') or '')
            if same_skill and g and h and len(g & h) / len(g | h) >= 0.5 and (k1, k2) not in seen:
                seen.add((k1, k2))
                lines.append(f"- MERGE? {k1} and {k2} say nearly the same thing in {Path((d1.get('applied') or {}).get('method_path', '')).parent.name}")
    return '\n'.join(lines)


def task_type_for(request):
    text = request.casefold()
    if any(w in text for w in ('basket', '篮子', 'workbook')):
        return 'basket_verification' if any(w in text for w in ('verify', 'verification', 'critique')) else 'basket_build'
    if any(w in text for w in ('implement', 'debug', 'code', 'fix', 'refactor')): return 'engineering'
    if any(w in text for w in ('research', 'sources', 'evidence', '研究')): return 'research'
    return 'other'
