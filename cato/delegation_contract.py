"""Brief-quality rules for Cato's delegations: the parts of "give clear orders" that can be enforced.

A chief of staff's brief fails in four ways that have nothing to do with wording: too many jobs in one,
no arithmetic (units x unit cost vs budget), no priority, and not knowing what the delegate can do.
Cal #17 (2026-10-06) had all four with a perfectly articulate brief. These rules refuse such a brief
before dispatch; the work-delegation skill holds the judgment the rules cannot make, plus worked
examples under references/briefs/ that grow from the scorecard.
"""
import hashlib
import json
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent                      # D:\Agents\cato
SKILL = ROOT / 'skills' / 'work-delegation'
CARDS = SKILL / 'references' / 'delegates'
BRIEFS = SKILL / 'references' / 'briefs'
MAX_CRITICAL, MAX_ITEMS = 3, 6
SAVE_WORDS = re.compile(r'\b(save|saved|write|written|output|deliver|put|store|export)\w*\b', re.I)
WIN_PATH = re.compile(r'[A-Za-z]:\\[^\s"\'`<>|,;)\]]+')


def validate_items(items):
    """Numbered definition of done: {item, check, critical[, early]}. Critical first, at most MAX_CRITICAL
    critical and MAX_ITEMS total (more is two requests), and item 1 is the early judgment slice."""
    if not isinstance(items, list) or not items:
        return 'completion_items must be a non-empty list of {item, check, critical}.'
    for n, it in enumerate(items, 1):
        if (not isinstance(it, dict) or not str(it.get('item', '')).strip() or not str(it.get('check', '')).strip()
                or not isinstance(it.get('critical'), bool)):
            return f'completion item {n} needs item, check (how it is verified) and critical (true/false).'
    flags = [it['critical'] for it in items]
    if not any(flags):
        return 'At least one completion item must be critical; otherwise nothing defines done.'
    if flags != sorted(flags, reverse=True):
        return 'List critical completion items first; the delegate works them in order.'
    if sum(flags) > MAX_CRITICAL or len(items) > MAX_ITEMS:
        return (f'{sum(flags)} critical / {len(items)} items: more than {MAX_CRITICAL} critical or {MAX_ITEMS} total is several jobs in one. '
                'Split into sequential requests; write the second brief after the first result is accepted.')
    if items[0].get('early') is not True:
        return ('Item 1 must be the early judgment slice (early: true): the smallest piece whose result lets you judge the whole '
                'job, e.g. "exposure facts for the 8 tier-one names". The delegate finishes and checkpoints it first.')
    from agent_core.verification import validate_items as validate_checks
    try:
        validate_checks(items)
    except ValueError as exc:
        return str(exc)
    return None


def guesses_refusal(guesses, verdict):
    """'' when acceptance may proceed, else the text refusing it: a delegate that stated assumptions gets a verdict on them."""
    real = [g for g in (guesses or []) if str(g).strip().lower() not in ('none', '')]
    if not real:
        return ''
    text = str(verdict or '').strip()
    if text.lower() == 'ok' or text.lower().startswith('wrong:'):
        return ''
    return ("accept needs guesses_verdict for the delegate's stated assumptions: 'ok' if each matches Alex's intent, or "
            "'wrong: <which assumption and what was meant>' (then rework the items it affected). Assumptions: "
            + '; '.join(str(g)[:140] for g in real))


def load_card(agent):
    """What the delegate can do: writable paths, tools, sources, limits. {} when no card exists."""
    path = CARDS / f'{agent}.json'
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _under(path, roots):
    p = path.lower().rstrip('\\')
    return any(p == r.lower().rstrip('\\') or p.startswith(r.lower().rstrip('\\') + '\\') for r in roots)


def path_refusal(agent, card, output_path='', *texts):
    """Refuse a brief that tells the delegate to save somewhere it cannot write (Cal #17: Cato's scratch)."""
    roots = card.get('writable_paths') or []
    if not roots:
        return None
    if output_path and not _under(output_path, roots):
        return f"{agent} cannot write to {output_path}. Its writable paths: {', '.join(roots)}. Point output_path there; you read it from there."
    for text in texts:
        for match in WIN_PATH.finditer(text or ''):
            path = match.group(0)
            if not path.lower().startswith('d:\\agents\\') or _under(path, roots):
                continue
            before = text[max(0, match.start() - 80):match.start()]
            if SAVE_WORDS.search(before):
                return (f"The brief tells {agent} to save to {path}, which it cannot write. Writable: {', '.join(roots)}. "
                        'Fix the path (Cal #17 spent part of its budget discovering this).')
    return None


# ── The decision to delegate at all ─────────────────────────────────────────────────────────────
# Multi-agent costs ~15x a single agent's tokens (Anthropic) and matches a single agent at equal budget
# unless the work is parallel across many units, exceeds one context window, needs a capability the
# orchestrator lacks, or needs a check it cannot do on itself (2026 equal-budget studies). Alex: Cato
# must make that call, not delegate by reflex. So the call is written down and priced.


def append_lesson(request_id, agent, lesson, items_unmet):
    """Counter-examples accumulate where Cato reads before writing the next brief."""
    BRIEFS.mkdir(parents=True, exist_ok=True)
    path = BRIEFS / 'lessons.md'
    if not path.exists():
        path.write_text('# Brief lessons (written by Cato at acceptance when a critical item was not met)\n\n', encoding='utf-8')
    with path.open('a', encoding='utf-8') as handle:
        handle.write(f"- {time.strftime('%Y-%m-%d')} {agent} {request_id} (unmet items {items_unmet}): {lesson.strip()}\n")
    return path


def save_exemplar(row, kind='accepted'):
    """Store a scorecard row's brief as a worked example (accepted = first-pass, critical items all met)."""
    folder = BRIEFS / kind
    folder.mkdir(parents=True, exist_ok=True)
    request = json.loads(row.get('brief') or '{}')
    items = json.loads(row.get('items') or '[]')
    path = folder / f"{row['request_id']}.md"
    lines = [f"# {request.get('title', row['request_id'])}", '',
             f"agent: {row.get('agent')} | type: {row.get('task_type')} | budget {row.get('budget') or 0:,} | spent {row.get('spent') or 0:,} "
             f"| items met {row.get('items_met')}/{row.get('items_total')} | reworks {row.get('reworks') or 0}", '',
             '## Completion items', '']
    lines += [f"{n}. {'[critical] ' if it.get('critical') else ''}{'[early] ' if it.get('early') else ''}{it.get('item')} (check: {it.get('check')})"
              for n, it in enumerate(items, 1)]
    lines += ['', '## Sizing', '', json.dumps(request.get('sizing'), ensure_ascii=False), '', '## Brief', '', request.get('question', '')]
    if row.get('brief_lesson'):
        lines += ['', '## Lesson', '', row['brief_lesson']]
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return path
