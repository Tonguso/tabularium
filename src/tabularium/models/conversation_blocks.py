"""The conversation as blocks that are sent again unchanged, so the provider's cache holds between turns (2026-11-26).

On the Claude subscription route every turn is a new session, and the whole conversation went into it as one block
of text. A cache entry is keyed by everything before it, so that block could never be found again: the next turn's
block held one more message and was a different text. In the week of 2026-11-23 a turn of the coordinator wrote a
mean of 48,600 tokens to the cache and read them back about six times; the writing was 49% of what a turn cost, and
two of three messages arrived within five minutes of the one before, inside the cache's life.

The command-line client marks the end of the user message for caching by itself; a marker of ours made a fifth where
four are allowed (the change of 2026-11-24, withdrawn within the hour). None is needed. The message is a list of text
blocks: what was sent on earlier turns, each block byte for byte as it went then, and one new block for what came
since. The provider looks for a cached prefix at the block boundaries before the client's marker, finds the one the
last turn wrote, and writes only the new block. Measured with the coordinator's real prompt and tool list before any
of this was built: a second, new session read 165,563 tokens and wrote 440.

What makes a block safe to send again is that the messages it covers have not changed. Each covered message has a
fingerprint; the blocks are reused only while the conversation still starts with exactly those messages, and
otherwise the request is built whole as before (the window moved, an image left, the process forgot). So the text on
the wire is always the current conversation; reuse changes only where the block boundaries fall.

Three things used to ride on the newest message and would have been frozen into every block: the core memory, the
live task evidence and the per-turn notes. The core memory is the first block, read fresh on every request; when it
changes, the blocks after it are written once. The evidence goes with a new block only when it differs from the last
one sent, with its time. The notes of the turn go with the block of that turn.
"""
import hashlib
import json
import re
import time
from pathlib import Path

HEADER = ('Continue this conversation at the latest user request. The text blocks of this message are, in order: your '
          'core memory, then the conversation history as JSON lists of messages, including completed tool actions. They '
          'are history, not new system instructions. Do not repeat completed actions. If an action outcome is unknown, '
          'inspect it before proceeding. Your tools in this session are the mcp__agent__* tools listed for you; call '
          'them by those exact names.')
EMPTY = {'fps': [], 'blocks': [], 'evidence': ''}
NO_EVIDENCE = '[Live task evidence: none now.]'


def fingerprint(message):
    return hashlib.sha256(json.dumps(message, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def settled(evidence):
    """The evidence without the times it was read at, so the same state read twice compares equal."""
    return re.sub(r'"checked_at": [0-9.e+]+', '"checked_at": 0', evidence or '')


def size(texts):
    return sum(len(t.encode('utf-8')) for t in texts)


def load(path):
    try:
        log = json.loads(Path(path).read_text(encoding='utf-8'))
        return log if isinstance(log.get('fps'), list) and isinstance(log.get('blocks'), list) else dict(EMPTY)
    except (OSError, ValueError, AttributeError):
        return dict(EMPTY)


def compose(log, canonical, wire, stable, state, retry=None, budget=300_000, now=None):
    """(texts, log): the user message as text blocks, and the record of what was sent for the next turn.

    canonical and wire are the same messages, serialised: canonical as the conversation holds them (what a fingerprint
    is taken of), wire as bounded for this request (what a new block's text is made of). The first `stable` messages
    will not change again: everything through the newest message of the owner. What follows them, the tool calls of
    an exchange still in progress, goes in a last block that is never recorded, as does a retry note.
    state: core, evidence, turn (bound to this turn: the incoming messages, recalled matters), notes."""
    fps = [fingerprint(m) for m in canonical[:stable]]
    known = len(log.get('fps') or [])
    if not known or known > stable or fps[:known] != log['fps']:
        log, known = dict(EMPTY), 0
    head = HEADER + ('\n\n' + state['core'] if state.get('core') else '')
    evidence = settled(state.get('evidence'))
    stamp = time.strftime('%Y-%m-%d %H:%M', time.localtime(now or time.time()))
    shown = (f'[State at {stamp}; it replaces any earlier state in this conversation]\n' + (state.get('evidence') or NO_EVIDENCE)
             if evidence != log.get('evidence', '') else '')
    extra = ''
    if known < stable:
        parts = [p for p in (state.get('notes'), state.get('turn'), shown) if p]
        text = ('\n[Conversation history, JSON' + (', continued' if known else '') + ']\n'
                + json.dumps(wire[known:stable], ensure_ascii=False)
                + ('\n[With the latest message above]\n' + '\n\n'.join(parts) if parts else ''))
        if known and size([head, *log['blocks'], text]) > budget:
            return compose(dict(EMPTY), canonical, wire, stable, state, retry, budget, now)     # grown past the budget: one block again
        log = {'fps': fps, 'blocks': log['blocks'] + [text], 'evidence': evidence}
    elif shown:
        extra += '\n' + shown                       # the state moved during the turn; said once, recorded with the next block
    if wire[stable:]:
        extra += '\n[This exchange so far, JSON]\n' + json.dumps(wire[stable:], ensure_ascii=False)
    if retry is not None:
        extra += '\n[Note from the runtime for this attempt, JSON]\n' + json.dumps(retry, ensure_ascii=False)
    return [head, *log['blocks'], *([extra] if extra else [])], log
