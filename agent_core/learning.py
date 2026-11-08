"""Lessons: one flow, Alex's (2026-10-12).

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
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from .project_store import ProjectStore

OPEN = ("candidate", "proposed", "applied")
LEDGER = "## Lessons"
LEDGER_NOTE = ("Entries added by approved lessons (agent_core.learning). Each is checked on later tasks and counted; "
               "retired entries are removed. Edit this section only through the lesson tool.")


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
        status='candidate', task_types=[task_type]))
    from agent_core import lesson_cases              # every lesson carries its own benchmark case (2026-10-26)
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
    """The trial record on a proposed lesson (agent_core.trials writes it; the model cannot)."""
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


def propose_change(root, chat, key, method_path, current_text, new_text):
    """Step 2: the exact edit Alex will see. current_text must occur exactly once in the skill file (empty = append
    new_text at the end). The file's hash is kept, so the edit applied later is the edit he saw."""
    method = _skill_file(root, method_path)
    text = method.read_text(encoding='utf-8')
    if not (new_text or '').strip(): raise ValueError('new_text is empty.')
    if current_text and text.count(current_text) != 1:
        raise ValueError('current_text must appear exactly once in the skill file; quote it exactly.')

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
        case = d.get('case') or {}
        if case.get('status') == 'draft' and not case.get('requirements'):
            case['rule'] = ' '.join(new_text.split())     # the case states the rule as the skill will carry it (2026-11-02)
    _update(root, chat, key, change)
    shown = (f"Proposed change to {method.name} ({method.parent.name}):\n"
             + (f"REPLACE:\n{current_text}\nWITH:\n{new_text}" if current_text else f"ADD AT THE END:\n{new_text}"))
    return {'key': key, 'status': 'proposed', 'show_owner': shown}


def record(root, chat, content, source, method_path, current_text, new_text, task_type='other'):
    """One step: the lesson and the exact skill edit it proposes, registered for Alex's go. A lesson without its edit
    is not recorded (2026-11-01: 183 candidates had been recorded, and of the last 41 only 6 ever got as far as a
    draft; Alex: [quote removed]). The edit is checked before anything is saved."""
    method = _skill_file(root, method_path)
    text = method.read_text(encoding='utf-8')
    if not (new_text or '').strip(): raise ValueError('A lesson is recorded with its edit: new_text is empty.')
    if current_text and text.count(current_text) != 1:
        raise ValueError('current_text must appear exactly once in the skill file; quote it exactly.')
    key = propose(root, chat, content, source, task_type)
    return propose_change(root, chat, key, method, current_text, new_text)


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
        shown = (f"REPLACE:\n{p['current_text']}\nWITH:\n{p['new_text']}" if p.get('current_text')
                 else f"ADD AT THE END:\n{p.get('new_text', '')}")
        if trial.get('notified') and trial.get('report'):
            try: shown += '\n\nTRIAL DONE:\n' + Path(trial['report']).read_text(encoding='utf-8')[:3000]
            except OSError: shown += '\n\nTRIAL DONE (report file missing).'
        out.append(dict(key=key, title=(d.get('title') or '')[:200], skill=skill, at=p.get('at') or d.get('_updated', 0), shown=shown))
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
    return _bump_version(edited)


def approve(root, chat, key, source_id, quote, skip_trial=False, reason=''):
    """Step 3: Alex's go, in his own words (message id + exact quote, checked against the conversation record),
    applies exactly the proposed edit and bumps the skill's patch version. Status 'applied'. A finished trial
    (agent_core.trials) comes first, so he judges independent checks and actual outputs rather than the argument for the edit; skip_trial
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
        from agent_core.trials import evidence_matches
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
        # A replacement sharpens an existing rule in place; an addition becomes one ledger entry (ACE, 2026-10-24).
        method.write_text(render_change(text, key, p), encoding='utf-8')
        d['status'] = 'applied'
        d['approval'] = {'source': f'message:{int(source_id)}', 'quote': quote, 'at': time.time()}
        d['applied'] = {'method_path': str(method), 'method_sha256': _sha(method), 'at': time.time(),
                        'entry': not p['current_text']}
        d.setdefault('helped', 0); d.setdefault('hurt', 0)
        return {'key': key, 'status': 'applied', 'skill': str(method)}
    return _update(root, chat, key, change)


GO = re.compile(r"^Skill edit (lesson-\w+): (apply it, no trial needed|apply it|drop it)\.?\s*$")


def settle_go(root, chat, key, verdict, source_id, quote, store, system_prompt=''):
    """Alex's verdict on a proposed edit, settled by the runtime the moment it arrives (2026-11-08: 25 edits he had
    said "apply it" to stood on his list for a day, because applying depended on the agent running a trial that no
    skill had cases for, then calling approve). drop: retired. apply, no trial: applied. apply: the trial runs
    when the skill has benchmark cases and the edit returns to his list with the report; when it has none, the
    edit is applied and the record says why no trial ran. Returns one line for him; ValueError when nothing changed."""
    if verdict == 'drop it':
        out = retire(root, chat, key, source_id, quote)
        return f"Skill edit {key} retired. {out['note']}".strip()
    if verdict == 'apply it, no trial needed':
        out = approve(root, chat, key, source_id, quote, skip_trial=True, reason='Alex: no trial needed')
        return f"Skill edit {key} applied to {Path(out['skill']).name}, no trial."
    from agent_core import trials
    if trials.evidence_matches(read(root, chat, key)):
        out = approve(root, chat, key, source_id, quote)
        return f"Skill edit {key} applied to {Path(out['skill']).name} after its trial."
    try:
        out = trials.start(root, chat, key, {}, store, system_prompt)
        return (f"Skill edit {key}: trial queued, {len(out['tasks'])} runs. It comes back to your list with the report; "
                "the skill is unchanged until then.")
    except ValueError as exc:
        reason = f'no trial possible: {exc}'
    out = approve(root, chat, key, source_id, quote, skip_trial=True, reason=reason)
    return f"Skill edit {key} applied to {Path(out['skill']).name}; {reason}"


def record_check(root, chat, key, worked, evidence):
    """Step 4: after a real task where the failure could have recurred. Every check counts for the entry (helped or
    hurt). An applied lesson's first success makes it 'verified'; a failure before any success sends it back to
    'candidate'. A verified entry stays in the ledger and keeps being counted; when it has hurt more than it helped the
    digest offers it for retirement (ACE, 2026-10-24). The check carries evidence; the agent's opinion is not a count."""
    if not (evidence or '').strip(): raise ValueError('A check needs evidence: the task and what happened.')

    def change(d):
        if d.get('status') not in ('applied', 'verified'): raise ValueError('Only an applied or verified lesson can be checked.')
        d.setdefault('checks', []).append({'worked': bool(worked), 'evidence': evidence, 'at': time.time()})
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
    return '\n'.join(f"- [{key}] ({TAGS[d['status']]}) {d.get('candidate_change') or d.get('title')}" for key, d in rows[:limit])


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


TOOL = {"name":"lesson","description":"Turn a recorded lesson into a skill change, Alex's way: show him the change, apply it on his go, then check it worked. propose: re-draft the edit of a lesson already recorded (a new lesson is recorded with its edit in one step by remember kind=lesson): current_text quoted exactly as it appears once in the file to sharpen an existing rule, or empty to add one entry to the skill's '## Lessons' ledger; new_text in the skill's own voice. The runtime shows every proposal to Alex on the dashboard and in a weekly message. Every check on a later task counts for the entry (helped/hurt); retire drops a proposed edit, or removes an applied ledger entry, after Alex says so in his own words (the digest names entries that hurt more than they helped). A dashboard verdict ('Skill edit <key>: apply it' / 'apply it, no trial needed' / 'drop it') is settled by the runtime itself before any turn; you see the outcome in the conversation and do nothing for it. The result's show_owner is what he must see before anything changes. trial: compare frozen skills on reviewed benchmark cases using disjoint development_cases and heldout_cases IDs, repetitions and max_steps. Choose relevant cases. Exposed cases cannot be reused as held-out evidence. The report checks actual files and records usage; small samples are screening, never proof of general improvement. Run it after propose unless Alex says no trial is needed. approve: only after Alex says yes in his own words, normally after seeing the trial; pass his message id and the exact quote, and skip_trial=true only if he said no trial is needed. The runtime applies exactly the proposed edit and bumps the skill's version; you never edit the file yourself for a lesson. checked: after the next real task where the failure could have recurred, record worked=true/false with evidence (the task and what happened). A recurrence sends the lesson back to candidate. Every lesson also carries a drafted benchmark case (its task's request and Alex's rule): case: fill in the oracle_basis and the requirements (benchmark format: id, description, check of kind json/text/csv/xlsx/judgment with a relative path and expected value) so code can grade it; calibrate: three isolated runs per arm with the lesson's skill, the request alone and the request with the rule stated, after which the runtime refuses a case that mostly fails even with the rule or mostly passes without it; approve_case: Alex's quoted yes adds a calibrated case to the benchmark; recombine: draft a reserved (held-out) case from this and one or two other approved cases of the same skill, then fill/calibrate it. A trial with no chosen cases uses the skill's approved development and reserved cases.",
  "input_schema":{"type":"object","properties":{"action":{"type":"string","enum":["propose","trial","approve","checked","retire","case","calibrate","approve_case","recombine"]},"slot":{"type":"string","enum":["case","reserved"]},"request":{"type":"string","description":"case: the task as Alex would ask it (defaults to the lesson's task request)"},"oracle_basis":{"type":"string","description":"case: how the expected answer was established, independent of the agent"},"requirements":{"type":"array","items":{"type":"object"},"description":"case: benchmark requirements, each with id, description and a check"},"with_keys":{"type":"array","items":{"type":"string"},"description":"recombine: one or two other lesson keys of the same skill"},"key":{"type":"string"},"development_cases":{"type":"array","items":{"type":"string"}},"heldout_cases":{"type":"array","items":{"type":"string"}},"repetitions":{"type":"integer","minimum":1,"maximum":5},"max_steps":{"type":"integer","minimum":1,"maximum":60},"skip_trial":{"type":"boolean","description":"approve: only when Alex said no trial is needed"},"method_path":{"type":"string","description":"propose: the SKILL.md or references file under skills/"},"current_text":{"type":"string"},"new_text":{"type":"string"},"source_message_id":{"type":"integer"},"quote":{"type":"string"},"worked":{"type":"boolean"},"evidence":{"type":"string"}},"required":["action","key"]}}


def execute(agent, chat, inputs):
    """The lesson tool for any agent whose role learns (Cato since 2026-10-10; Cal and Vit since 2026-10-20, when
    Alex's corrections started producing diagnosed lessons for them too)."""
    import json
    try:
        action = inputs.get("action")
        if action == "propose":
            return json.dumps(propose_change(agent.AGENT_DIR, chat, inputs["key"], inputs["method_path"],
                                             inputs.get("current_text", ""), inputs["new_text"]), ensure_ascii=False)
        if action == "trial":
            from agent_core import trials
            return json.dumps(trials.start(agent.AGENT_DIR, chat, inputs["key"], inputs, agent._background, agent.SYSTEM_PROMPT),
                              ensure_ascii=False)
        if action == "approve":
            return json.dumps(approve(agent.AGENT_DIR, chat, inputs["key"], inputs["source_message_id"],
                                      inputs["quote"], skip_trial=bool(inputs.get("skip_trial"))), ensure_ascii=False)
        if action == "checked":
            out = record_check(agent.AGENT_DIR, chat, inputs["key"], inputs["worked"], inputs["evidence"])
            if not inputs["worked"]:                    # a recurrence is a failure the diagnosis should read (2026-10-24)
                from agent_core import corrections
                corrections.start_failure(agent, chat, f"Lesson {inputs['key']} recurred", inputs["evidence"])
            return json.dumps(out, ensure_ascii=False)
        if action == "retire":
            return json.dumps(retire(agent.AGENT_DIR, chat, inputs["key"], inputs["source_message_id"], inputs["quote"]),
                              ensure_ascii=False)
        from agent_core import lesson_cases
        if action == "case":
            return json.dumps(lesson_cases.fill(agent.AGENT_DIR, chat, inputs["key"], inputs.get("slot", "case"),
                                                inputs.get("request"), inputs.get("oracle_basis"), inputs.get("requirements")), ensure_ascii=False)
        if action == "calibrate":
            return json.dumps(lesson_cases.calibrate(agent.AGENT_DIR, chat, inputs["key"], agent._background, agent.SYSTEM_PROMPT,
                                                     inputs.get("slot", "case"), inputs.get("max_steps", 12)), ensure_ascii=False)
        if action == "approve_case":
            return json.dumps(lesson_cases.approve(agent.AGENT_DIR, chat, inputs["key"], inputs["source_message_id"], inputs["quote"],
                                                   inputs.get("slot", "case")), ensure_ascii=False)
        if action == "recombine":
            return json.dumps(lesson_cases.recombine(agent.AGENT_DIR, chat, inputs["key"], inputs.get("with_keys") or []), ensure_ascii=False)
        return "Use action propose, trial, approve, checked, retire, case, calibrate, approve_case or recombine."
    except (ValueError, KeyError, TypeError) as exc:
        return f"Not recorded: {exc}"


EXPIRE_DAYS = 30


def expire(root, chat, days=EXPIRE_DAYS, now=None):
    """Candidates untouched for `days` become 'expired': out of hints and out of the backlog, still in history.
    A repeat of an expired lesson creates a fresh candidate, which is the signal that it mattered (2026-10-20)."""
    now = now or time.time()
    gone = []
    for key, d in _lessons(root, chat, ('candidate',)):
        if now - d.get('_updated', now) > days * 86400:
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
    groups = []                                   # [(representative, [keys])] by bigram similarity
    for key, d in cands:
        text = d.get('candidate_change') or d.get('title') or ''
        g = _grams(text)
        for group in groups:
            h = _grams(group[0][1].get('candidate_change') or group[0][1].get('title') or '')
            if g and h and len(g & h) / len(g | h) >= 0.3:
                group[1].append(key); break
        else:
            groups.append(((key, d), [key]))
    groups.sort(key=lambda g: (-len(g[1]), -g[0][1].get('_updated', 0)))
    lines = [f"Lessons: {len(proposed)} proposed, {len(applied)} applied (awaiting a check), {len(cands)} candidates in {len(groups)} themes."]
    for key, d in proposed:
        lines.append(f"- PROPOSED {key}: {(d.get('title') or '')[:120]}  (awaiting your go; a trial runs first)")
    for (key, d), members in groups[:limit]:
        lines.append(f"- x{len(members)} {key}: {(d.get('candidate_change') or d.get('title') or '')[:140]}")
    if len(groups) > limit:
        lines.append(f"- and {len(groups) - limit} more themes, each seen once; they expire after {EXPIRE_DAYS} days unless repeated.")
    from agent_core import lesson_cases                 # cases awaiting his yes, and drafts the agent still owes
    lines.extend(lesson_cases.digest_lines(rows))
    from agent_core import regression
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
