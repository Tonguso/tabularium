"""Current memory is a projection: durable Markdown facts plus transactional working state.

The original Markdown is retained. Its thread/activity sections seed the existing project
database once, then remain historical snapshots rather than a second writable state store.
"""
from contextlib import closing, contextmanager
from datetime import datetime
import json
from pathlib import Path
import re
from tabularium.memory.memory_store import current_facts
import sqlite3

MARKER = '<!-- MEMORY-DETAIL-BELOW'
SECTIONS = re.compile(r'(?ms)^## ([^\n]+)\n(.*?)(?=^## |\Z)')
THREAD = re.compile(r'^- (?:\[(?P<flags>[^]]*)\] )?\[(?P<name>[^]]+)\] (?P<state>.*) \((?P<date>\d{4}-\d{2}-\d{2})\)$')
STATE_TITLES = ('Open Threads', 'Parked Threads', 'Recent Activity', 'Activity Archive')
POLICY_TITLES = ('Standing Instructions', 'Model Routing Policy', 'Restart procedure', 'Audited local delegation', 'Work delegation skill', 'Delegate Budgeting Rebuild')
AFFAIR = re.compile(r'^- \*\*(?P<name>[^*]+)\*\* \[keywords: (?P<keywords>[^\]]+)\]')


def affairs(root):
    """The standing matters of Alex's life, from the core memory section "Alex's Affairs": (name, keywords) per
    entry. A message that names one of the keywords arrives with that matter's newest archived turns, so a matter
    does not vanish when the conversation window rolls past it (2026-11-01: a month of flat-purchase talk was in the
    archive, the newest turn 32 minutes before the window began, and Cato asked Alex who the buyer was)."""
    p = memory_path(Path(root))
    text = p.read_text(encoding='utf-8-sig') if p.exists() else ''
    found = []
    for section in SECTIONS.finditer(text):
        title, body = section.groups()
        if not title.startswith("Alex's Affairs"):
            continue
        for line in body.splitlines():
            m = AFFAIR.match(line.strip())
            if m:
                found.append((m.group('name').strip(),
                              [k.strip().lower() for k in m.group('keywords').split(',') if k.strip()]))
    return found

class WorkingMemory:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / 'task_state' / 'projects.sqlite3'

    @contextmanager
    def transaction(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS memory_state(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS memory_events(id INTEGER PRIMARY KEY, created TEXT, kind TEXT, value TEXT)')
                db.execute('BEGIN IMMEDIATE')
                row = db.execute("SELECT value FROM memory_state WHERE key='working'").fetchone()
                value = json.loads(row[0]) if row else self.seed()
                yield db, value
                encoded=json.dumps(value, ensure_ascii=False)
                if not row or encoded != row[0]:
                    db.execute("INSERT OR REPLACE INTO memory_state VALUES('working',?)", (encoded,))
        finally:
            db.close()

    def seed(self):
        p = self.root / 'memory.md'
        text = p.read_text(encoding='utf-8-sig') if p.exists() else ''
        result = dict(threads=[], blocked=[], parked=[], activity=[], archive=[])
        for section in SECTIONS.finditer(text):
            title, body = section.groups()
            if title.startswith('Open Threads'):
                for line in body.splitlines():
                    if line.startswith('- NOT TRACKING '):
                        result['blocked'] = [n.strip().lower() for n in line.split(':',1)[1].split(',') if n.strip()]
                    m = THREAD.match(line.strip())
                    if m:
                        item=m.groupdict(); item['pinned']='P' in (item.pop('flags') or '')
                        result['threads'].append(item)
            for prefix,key in [('Parked Threads','parked'),('Recent Activity','activity'),('Activity Archive','archive')]:
                if title.startswith(prefix):
                    result[key] = [line for line in body.splitlines() if line.startswith('- ')]
        return result

    def snapshot(self):
        with self.transaction() as (_, value): return value

    def thread(self, name, state='', done=False, pinned=False, owner_asked=False):
        name = (name or '').strip()[:40]
        if not name: return 'A thread needs a name.'
        key=name.lower()
        with self.transaction() as (db,v):
            threads=v['threads']; blocked=set(v['blocked'])
            old=next((t for t in threads if t['name'].lower()==key),None)
            if done:
                v['threads']=[t for t in threads if t['name'].lower()!=key]
                if owner_asked: blocked.add(key)
                note=f"Closed '{name}'."
            else:
                if key in blocked and not owner_asked: return f"'{name}' is NOT TRACKING by Alex's instruction; do not re-add without his request."
                state=re.sub(r'\s+',' ',state or '').strip()[:200] or (old['state'] if old else '')
                if not state: return 'A thread needs its position and next action.'
                updated=[t for t in threads if t['name'].lower()!=key]
                updated.insert(0,dict(name=name,state=state,date=datetime.now().strftime('%Y-%m-%d'),pinned=bool(pinned or owner_asked or (old and old['pinned']))))
                parked=[]
                while len(updated)>10:
                    victim=next((t for t in reversed(updated) if not t['pinned'] and t['name'].lower()!=key),None)
                    if victim is None: return 'All working slots are pinned; ask Alex which thread to park.'
                    updated.remove(victim); parked.append(victim)
                v['threads']=updated
                v['parked']=parked+v['parked']
                blocked.discard(key)
                note=f"Tracking '{name}'."
            v['blocked']=sorted(blocked)
            db.execute('INSERT INTO memory_events(created,kind,value) VALUES(?,?,?)',
                       (datetime.now().isoformat(),'thread',json.dumps(v,ensure_ascii=False)))
            return note

    def activity(self, note):
        note=re.sub(r'\s+',' ',note or '').strip()
        if not note: return 'No activity supplied.'
        entry=f"- {datetime.now():%Y-%m-%d}: {note}"
        with self.transaction() as (db,v):
            db.execute('INSERT INTO memory_events(created,kind,value) VALUES(?,?,?)',(datetime.now().isoformat(),'activity',entry))
            v['activity'].insert(0,entry)
            v['archive']=v['activity'][4:]+v['archive']; v['activity']=v['activity'][:4]
        return 'Activity recorded. Continue the conversation without announcing the bookkeeping.'

    def unpin(self, name):
        with self.transaction() as (db,v):
            match=next((t for t in v['threads'] if t['name']==name),None)
            if not match: return 'Thread no longer exists; list current threads again.'
            match['pinned']=False
            db.execute('INSERT INTO memory_events(created,kind,value) VALUES(?,?,?)',
                       (datetime.now().isoformat(),'unpin',json.dumps(v,ensure_ascii=False)))
        return f"Unpinned '{name}'."

    def render(self, historical=False):
        v=self.snapshot()
        lines=['## Open Threads (current working state)']
        for t in v['threads']:
            lines.append(f"- {'[P] ' if t['pinned'] else ''}[{t['name']}] {t['state']} ({t['date']})")
        if v['blocked']: lines.append('- NOT TRACKING: '+', '.join(v['blocked']))
        lines+=['\n## Recent Activity']+[line[:240] for line in v['activity']]
        with closing(sqlite3.connect(self.path)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='records'").fetchone():
                projects=[]
                for key,raw in db.execute("SELECT key,data FROM records WHERE kind='project' ORDER BY updated DESC"):
                    record=json.loads(raw)
                    if record.get('status') in ('active','blocked'):
                        projects.append(f"- [{key}] {record['status']}: {record.get('title',key)}; next: {record.get('next_action','unspecified')}")
                if projects: lines+=['\n## Current projects (project register)']+projects[:10]
        if historical:
            lines+=['\n## Parked Threads']+[json.dumps(t,ensure_ascii=False) if isinstance(t,dict) else t for t in v['parked']]
            lines+=['\n## Activity Archive']+v['archive']
        return '\n'.join(lines)+'\n'

def without_state(text, policy=False):
    return SECTIONS.sub(lambda m: '' if m.group(1).startswith(STATE_TITLES+(POLICY_TITLES if policy else ())) else m.group(0),text)

def memory_path(root):
    """memory.md, or the candidate standing in for it during a consolidation trial (tabularium.learning.trials)."""
    from tabularium.learning import trials
    p=Path(root)/'memory.md'
    return Path(trials.overridden(p) or p)


def current_context(root, include_working=True):
    root=Path(root); p=memory_path(root)
    text=p.read_text(encoding='utf-8-sig') if p.exists() else ''
    prefix=text.split(MARKER)[0] if MARKER in text else text.split('## Detail')[0]
    core=without_state(prefix,policy=True).strip()
    # Sourced facts below the detail marker are otherwise invisible at session start.
    facts=[]
    today=datetime.now().strftime('%Y-%m-%d')
    current=current_facts(text)
    for match in re.finditer(r'(?ms)^### Fact ([0-9a-f]+)\n```json\n(.*?)\n```',current):
        try: record=json.loads(match.group(2))
        except ValueError: continue
        if record.get('valid_from') and record['valid_from']>today: continue
        if record.get('valid_to') and record['valid_to']<today: continue
        if record.get('kind') == 'user_preference':
            facts.append(f"- {record['content']} [fact {match.group(1)}; {record.get('evidence',{}).get('source','source unknown')}]")
    if facts: core+='\n\n## Sourced preferences\n'+'\n'.join(facts)
    return core + ('\n\n' + WorkingMemory(root).render() if include_working else '')

def read_view(root):
    root=Path(root); p=memory_path(root)
    text=p.read_text(encoding='utf-8-sig') if p.exists() else ''
    text=SECTIONS.sub(lambda m: '## Historical policy snapshot: '+m.group(1)+'\nNot current authority; consult POLICY.md and routing.json.\n'+m.group(2) if m.group(1).startswith(POLICY_TITLES) else m.group(0),text)
    return without_state(text)+'\n'+WorkingMemory(root).render(historical=True)


# One tool contract for every agent; callers receive separate dictionaries.
def memory_tool_schema():
    return {'name': 'remember',
     'description': 'Your one memory writer. '
                    'kind=user_decision|user_preference|assistant_proposal|observation|inference saves '
                    'a SOURCED fact: content + source (message:<archive id> from search_conversations, '
                    'or a saved task_state actions JSON path) + exact quote; the speaker and the quote '
                    'are checked, the truth is not; supersedes=<fact id> replaces a prior sourced fact '
                    'and keeps its history. kind=note appends a dated unsourced note (never for '
                    "Alex's decisions or preferences: those need a source and quote). kind=lesson "
                    'records a lesson: a claim about the decision in a method that allowed a failure, with the edit that removes it (source: message:<id> of the request that went wrong, or the task checkpoint path; content: the failure in one line; '
                    'cause: the decision that allowed it; anchor: the passage quoted exactly from the method, which old must contain, or silent_at: where the method says nothing; explains: keys of the other open observations this cause accounts for, one instance being enough only when the source is a task record or his message; '
                    'method_path: the skill file, or SKILL.md for a habit of conversation; '
                    'old: the current text to rewrite, or empty, only where the method is silent, to add a ledger entry; new: the new text in the '
                    "skill's own voice): nothing is recorded without the cause and the edit, and nothing changes until Alex "
                    'approves it; the runtime shows him the edit. A reminder to remember something is not a lesson: the edit removes the condition that made the failure possible. Use it whenever you find you would do something '
                    'differently next time, in the foreground too. kind=thread keeps working memory: '
                    'name, state (position + next step), done, pinned, owner_asked=true when Alex '
                    'asked to track or drop it. old+new corrects one exact existing sentence and '
                    'archives the old wording. Read the relevant section first (read_memory) so you do '
                    'not duplicate. Removing whole sections is a maintenance script Alex runs, not a '
                    'tool. kind=consolidate is for the weekly review clean-ups (merging duplicates, retiring '
                    'stale sections): old+new (new empty removes) accumulate into a candidate copy that is not '
                    'live; state=trial queues the memory exam on live and candidate alike (about two minutes); '
                    "state=status shows edits and scores; state=apply only after Alex's go; state=rebase when memory.md changed "
                    "since the candidate; state=discard drops it.",
     'input_schema': {'type': 'object',
                      'properties': {'kind': {'type': 'string',
                                              'enum': ['user_preference',
                                                       'user_decision',
                                                       'assistant_proposal',
                                                       'observation',
                                                       'inference',
                                                       'note',
                                                       'thread',
                                                       'lesson',
                                                       'consolidate']},
                                     'content': {'type': 'string'},
                                     'source': {'type': 'string'},
                                     'quote': {'type': 'string'},
                                     'valid_from': {'type': 'string',
                                                    'description': 'YYYY-MM-DD or empty; do not guess'},
                                     'valid_to': {'type': 'string',
                                                  'description': 'YYYY-MM-DD or empty'},
                                     'supersedes': {'type': 'string',
                                                    'description': 'Prior current fact ID, or empty'},
                                     'old': {'type': 'string',
                                             'description': 'Correction: exact current text occurring '
                                                            'once'},
                                     'new': {'type': 'string'},
                                     'reason': {'type': 'string'},
                                     'name': {'type': 'string',
                                              'description': 'thread: short stable id'},
                                     'state': {'type': 'string',
                                               'description': 'thread: position + next step'},
                                     'done': {'type': 'boolean'},
                                     'pinned': {'type': 'boolean'},
                                     'owner_asked': {'type': 'boolean'},
                                     'method_path': {'type': 'string',
                                                     'description': 'lesson: the skill file the edit changes'},
                                     'cause': {'type': 'string', 'description': 'lesson: the decision in the method that allowed the failure'},
                                     'anchor': {'type': 'string', 'description': 'lesson: the passage quoted exactly from the method; old must contain it'},
                                     'silent_at': {'type': 'string', 'description': 'lesson: where the method says nothing, when there is no anchor'},
                                     'explains': {'type': 'array', 'items': {'type': 'string'}, 'description': 'lesson: keys of the other open observations this cause accounts for'}},
                      'required': []}}
