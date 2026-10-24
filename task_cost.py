"""Provider usage and a separately labelled heuristic effort score, and the per-turn awareness line.

Nothing here stops work. Alex, 2026-10-08: no budget ceiling of any kind; the agent decides whether more
tokens are worth it and says so in the result; loops are stopped by the engine's detectors (identical-call
guard, runaway streak), not by a number. The cap-and-pause regime of 2026-10-06 to 10-08 stopped eleven
productive tasks and caught no loop, and project_budget.py, which carried it, was replaced by this module on
2026-10-09: no project database, no membership, no allowances.

Three sources, all already written by the task itself:
- spend: the task's usage_events (task_state/inbox.sqlite3), weighted;
- the agent's own estimate: checkpoint.json (task_checkpoint estimate_effective_tokens);
- a delegate's target: the contract in context.json (budget_effective_tokens from the brief).
"""
import json
import sqlite3
from pathlib import Path
from agent_core.usage import aggregate, input_total, decode

EFFECTIVE_WEIGHTS = {'input_uncached': 1.0, 'cache_write': 1.25, 'cache_read': 0.1, 'output': 5.0}
TASK_TYPES = ('basket_build', 'basket_verification', 'research', 'engineering', 'probe', 'other')
OVER_ESTIMATE_MULTIPLE = 2.0   # past this x the agent's own estimate the line asks for a value-for-cost decision


def effective_tokens(metrics):
    """Heuristic only, never billing or quota. Missing cache splits are conservatively estimated."""
    m = metrics or {}
    if m.get('input_uncached') is None and m.get('input_total') is not None:
        cached = m.get('cache_read') or 0
        m = dict(m, input_uncached=max(0, (m['input_total'] or 0) - cached - (m.get('cache_write') or 0)))
    return int(round(sum((m.get(k) or 0) * w for k, w in EFFECTIVE_WEIGHTS.items())))


def _read(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def contract(directory):
    """Budget metadata plus the task's durable requirements; old files are migration input only."""
    directory = Path(directory)
    value = _read(directory / 'context.json').get('contract') or {}
    path = directory.parent / 'jobs.sqlite3'
    if path.exists() and directory.name.isdecimal():
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
        try:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='requirements'").fetchone():
                row = db.execute("SELECT items FROM requirements WHERE task=? AND request=''", (int(directory.name),)).fetchone()
                if row is not None:
                    value['completion_items'] = json.loads(row[0])
        finally:
            db.close()
    return value


def checkpoint(directory):
    return _read(Path(directory) / 'checkpoint.json')


def target(directory):
    value = contract(directory).get('budget_effective_tokens')
    return int(value) if value else None


def set_target(directory, value):
    """Record (or raise) a delegate's target in its contract. Information for the worker's awareness line; nothing
    pauses at it. Returns the value."""
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError('Target must be a positive integer of effective tokens.')
    from agent_runtime import atomic_json
    path = Path(directory) / 'context.json'
    data = _read(path)
    data.setdefault('contract', {})['budget_effective_tokens'] = value
    atomic_json(path, data)
    return value


def reset_estimate(directory):
    """After a rescope the delegate must estimate the new scope again."""
    path = Path(directory) / 'checkpoint.json'
    data = checkpoint(directory)
    if 'estimate_effective_tokens' in data:
        data.pop('estimate_effective_tokens')
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')


def spend(directory):
    """Reported counters and heuristic score; unfinished tools are not additional model calls."""
    path = Path(directory) / 'task_state' / 'inbox.sqlite3'
    rows = []
    if path.exists():
        con = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
        try:
            role = "role" if "role" in {r[1] for r in con.execute("PRAGMA table_info(usage_events)")} else "''"
            rows = con.execute(f"SELECT kind, metrics, {role} FROM usage_events WHERE kind IN ('response','tool_started') ORDER BY id").fetchall()
        finally:
            con.close()
    records, pending = [], 0
    for kind, raw, role in rows:
        if kind == 'tool_started':
            pending += 1
        else:
            pending = 0
            records.append(decode(raw, role))
    out = aggregate(records)
    out.update(estimated_effective_tokens=sum(effective_tokens(m) for m in records),
               estimate_incomplete=bool(pending) or any(any(m.get(k) is None for k in EFFECTIVE_WEIGHTS) for m in records),
               unknown_usage=sum(input_total(m) is None or m.get('output') is None for m in records),
               turns=len(records), pending_tools=pending)
    return out


def status(directory):
    """Spend plus the target and estimate, for a client's status reply."""
    out = spend(directory)
    out['target'] = target(directory)
    out['own_estimate'] = checkpoint(directory).get('estimate_effective_tokens')
    if out['target']:
        out['ratio'] = round(out['estimated_effective_tokens'] / out['target'], 2)
    return out


def phase(directory, usage=None):
    """'over_target' past a delegate's target, 'over_estimate' past OVER_ESTIMATE_MULTIPLE x the agent's own estimate,
    else 'work'. None when the directory is not a task (no context.json: the foreground chat)."""
    if not (Path(directory) / 'context.json').exists():
        return None
    usage = usage or spend(directory)
    spent = usage['estimated_effective_tokens']
    goal = target(directory)
    if goal:
        return 'over_target' if spent >= goal else 'work'
    estimate = checkpoint(directory).get('estimate_effective_tokens')
    if estimate and spent >= estimate * OVER_ESTIMATE_MULTIPLE:
        return 'over_estimate'
    return 'work'


INSTRUCTION = {
    'work': 'Spend where another action could change the answer; before a costly step ask whether finishing is worth it, and if not, say so in the result and stop. Repeating a failing step is never worth it.',
    'over_estimate': 'You have spent twice your own estimate. Decide now: finish from what you have, or continue and state in the result why the extra spend was worth it. Repeating a failing step is never worth it.',
    'over_target': 'Past the budget target in the brief. Finish if finishing is worth it and report the cost against the target; do not stop for the number.'}


def notice(directory):
    """One awareness line per turn for a background task: spend so far, the agent's own estimate or the brief
    target. Requirements and proof appear only in the task contract. It informs the value-for-cost decision; it never stops anything. '' in the
    foreground chat. The '[Budget ' prefix is what the identical-call guard strips before hashing a result."""
    if not directory:
        return ''
    usage = spend(directory)
    current = phase(directory, usage)
    if current is None:
        return ''
    summary = {k: usage[k] for k in ('input_total', 'output', 'estimated_effective_tokens', 'estimate_incomplete', 'pending_tools')}
    goal = target(directory)
    summary['scope'] = 'request' if goal else 'task'
    if goal:
        summary['brief_target'] = goal
    plan = checkpoint(directory)
    if plan.get('estimate_effective_tokens'):
        summary['own_estimate'] = plan['estimate_effective_tokens']
    if plan.get('estimate_turns'):
        summary['planned_turns'] = plan['estimate_turns']
    return (f'[Budget {current}] ' + json.dumps(summary) + ' ' + INSTRUCTION[current]
            + ' Reported tokens and heuristic estimate; neither measures subscription quota. Pending work is unmeasured.')
