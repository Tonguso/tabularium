"""Lessons: one flow, Alex's (2026-10-12).

    record  ->  show the proposed improvement  ->  apply it after his go  ->  check whether it works
    candidate   proposed                          applied                    verified (or back to candidate)

Before this there were three paths that disagreed: approve a lesson and then separately approve a skill edit
(approve / mark_folded), an operator-only deploy that injected "validated guidance" beside the skill (deploy /
guidance, never used), and a weekly script that batched candidates into grouped changes (learning_loop.py). Now the
thing Alex approves is the exact edit, the runtime applies exactly that edit, and the lesson stays open until a real
task shows whether the failure recurred. A model cannot move a lesson past 'candidate' through project_record; only
these functions change its status, and approval needs Alex's own words from the conversation record.
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
    return key


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


def _skill_file(root, method_path):
    root = Path(root).resolve()
    method = Path(method_path)
    method = (method if method.is_absolute() else root / method).resolve()
    if not method.is_relative_to(root / 'skills') or not method.is_file():
        raise ValueError('method_path must be an existing file under this agent\'s skills folder.')
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
        d['status'] = 'proposed'
        d['proposal'] = {'method_path': str(method), 'current_text': current_text or '', 'new_text': new_text,
                         'file_sha256': _sha(method), 'at': time.time()}
    _update(root, chat, key, change)
    shown = (f"Proposed change to {method.name} ({method.parent.name}):\n"
             + (f"REPLACE:\n{current_text}\nWITH:\n{new_text}" if current_text else f"ADD AT THE END:\n{new_text}"))
    return {'key': key, 'status': 'proposed', 'show_owner': shown}


def _bump_version(text):
    def bump(m):
        parts = m.group(2).split('.')
        parts[-1] = str(int(parts[-1]) + 1) if parts[-1].isdigit() else parts[-1]
        return m.group(1) + '.'.join(parts) + m.group(3)
    head, sep, body = text.partition('\n---')
    return re.sub(r'(version:\s*"?)([0-9][0-9.]*)("?)', bump, head, count=1) + sep + body


def approve(root, chat, key, source_id, quote, skip_trial=False):
    """Step 3: Alex's go, in his own words (message id + exact quote, checked against the conversation record),
    applies exactly the proposed edit and bumps the skill's patch version. Status 'applied'. A finished trial
    (agent_core.trials) comes first, so he judges two real outputs rather than the argument for the edit; skip_trial
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
        if not (d.get('trial') or {}).get('done') and not skip_trial:
            raise ValueError('Run the trial first (lesson action=trial) so Alex sees both outputs, or pass skip_trial '
                             'only if he said no trial is needed.')
        if skip_trial and not (d.get('trial') or {}).get('done'):
            d['trial_skipped'] = {'quote': quote, 'at': time.time()}
        p = d['proposal']
        method = _skill_file(root, p['method_path'])
        if _sha(method) != p['file_sha256']:
            raise ValueError('The skill file changed since the proposal; propose the edit again so Alex sees the current version.')
        text = method.read_text(encoding='utf-8')
        text = text.replace(p['current_text'], p['new_text'], 1) if p['current_text'] else text.rstrip('\n') + '\n\n' + p['new_text'] + '\n'
        method.write_text(_bump_version(text), encoding='utf-8')
        d['status'] = 'applied'
        d['approval'] = {'source': f'message:{int(source_id)}', 'quote': quote, 'at': time.time()}
        d['applied'] = {'method_path': str(method), 'method_sha256': _sha(method), 'at': time.time()}
        return {'key': key, 'status': 'applied', 'skill': str(method)}
    return _update(root, chat, key, change)


def record_check(root, chat, key, worked, evidence):
    """Step 4: after a real task where the failure could have recurred. Worked: 'verified', the lesson closes.
    Recurred: back to 'candidate' with the evidence, so the next proposal starts from what did not work."""
    if not (evidence or '').strip(): raise ValueError('A check needs evidence: the task and what happened.')

    def change(d):
        if d.get('status') != 'applied': raise ValueError('Only an applied lesson can be checked.')
        d.setdefault('checks', []).append({'worked': bool(worked), 'evidence': evidence, 'at': time.time()})
        if worked:
            d['status'] = 'verified'
        else:
            d['status'] = 'candidate'
            d.pop('proposal', None)
        return {'key': key, 'status': d['status']}
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


TOOL = {"name":"lesson","description":"Turn a recorded lesson into a skill change, Alex's way: show him the change, apply it on his go, then check it worked. propose: the exact edit to one skill file (current_text quoted exactly as it appears once in the file, or empty to append; new_text in the skill's own voice). The result's show_owner is what he must see before anything changes. trial: rerun one finished research or basket task (task_id) twice in the background, once with the current skill and once with the proposed edit; nothing is sent or changed, and Alex receives both answers with a comparison when the second run ends. Run it after propose unless Alex says no trial is needed. approve: only after Alex says yes in his own words, normally after seeing the trial; pass his message id and the exact quote, and skip_trial=true only if he said no trial is needed. The runtime applies exactly the proposed edit and bumps the skill's version; you never edit the file yourself for a lesson. checked: after the next real task where the failure could have recurred, record worked=true/false with evidence (the task and what happened). A recurrence sends the lesson back to candidate.",
  "input_schema":{"type":"object","properties":{"action":{"type":"string","enum":["propose","trial","approve","checked"]},"key":{"type":"string"},"task_id":{"type":"integer","description":"trial: the finished past task to rerun, one where the lesson applies"},"skip_trial":{"type":"boolean","description":"approve: only when Alex said no trial is needed"},"method_path":{"type":"string","description":"propose: the SKILL.md or references file under skills/"},"current_text":{"type":"string"},"new_text":{"type":"string"},"source_message_id":{"type":"integer"},"quote":{"type":"string"},"worked":{"type":"boolean"},"evidence":{"type":"string"}},"required":["action","key"]}}


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
            return json.dumps(trials.start(agent.AGENT_DIR, chat, inputs["key"], inputs["task_id"], agent._background),
                              ensure_ascii=False)
        if action == "approve":
            return json.dumps(approve(agent.AGENT_DIR, chat, inputs["key"], inputs["source_message_id"],
                                      inputs["quote"], skip_trial=bool(inputs.get("skip_trial"))), ensure_ascii=False)
        if action == "checked":
            return json.dumps(record_check(agent.AGENT_DIR, chat, inputs["key"], inputs["worked"],
                                           inputs["evidence"]), ensure_ascii=False)
        return "Use action propose, trial, approve or checked."
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
    rows = _lessons(root, chat, OPEN)
    proposed = [(k, d) for k, d in rows if d.get('status') == 'proposed']
    applied = [(k, d) for k, d in rows if d.get('status') == 'applied']
    cands = [(k, d) for k, d in rows if d.get('status') == 'candidate']
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
    return '\n'.join(lines)


def task_type_for(request):
    text = request.casefold()
    if any(w in text for w in ('basket', '篮子', 'workbook')):
        return 'basket_verification' if any(w in text for w in ('verify', 'verification', 'critique')) else 'basket_build'
    if any(w in text for w in ('implement', 'debug', 'code', 'fix', 'refactor')): return 'engineering'
    if any(w in text for w in ('research', 'sources', 'evidence', '研究')): return 'research'
    return 'other'
