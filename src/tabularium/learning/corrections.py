"""When Alex corrects an agent, the agent works out the specifics, not Alex (2026-10-20; redesigned 2026-10-22).

Alex: [quote removed] So the reply envelope names the incoming message IDs that corrected the agent
(`correction`), and the runtime, not the model's memory of a rule, starts a background diagnosis: what happened, from
the conversation and the task records; what he expected; why; and whose it is. A fault in the agent's own method
becomes a lesson with the exact skill edit, registered for his go. A fault in the runtime becomes a report for
Claude Code or Codex in <agent>/runtime_reports/. He never has to spell the error out.

2026-10-22: the first design made every correction a job that ended in a message to him. One evening of arguing
about a formula produced twelve "[Correction diagnosis]" tasks, each ending in "是否批准...？是／否", and he cancelled
them all: "Why did Cato have so many useless background stuff that needs my attention"; "你的skills不应该是用来打补丁".
Two decisions were wrong. One diagnosis per message: a burst of corrections in one conversation is one lesson, so
corrections now collect in task_state/corrections_pending.json and one diagnosis starts when the thread has been
quiet for QUIET_SECONDS, with every correction in it. And the diagnosis spoke to him: now it never does. Nothing it
produces is delivered (notify_telegram=False) and it cannot ask (task_decisions.finish): the proposed edit waits in
the lesson register, and he reads the digest when he wants to (tabularium lessons; or asks the agent)."""
import json
import time

from tabularium.core.agent_runtime import atomic_json

PREFIX = "[Correction diagnosis]"
QUIET_SECONDS = 600            # a thread is over when Alex has not corrected for this long
BURST_RECENT_SECONDS = 180     # a burst note applies to replies within this long of the latest correction

BURST_NOTE = ("[Runtime note: Alex has corrected you {n} times in the last {minutes} minutes. Do not produce, fix, compute, "
              "propose or explain anything in this reply. In two or three lines, using the words he has used, say what you "
              "now understand he means and wants, then ask whether that is right. Nothing else.]")

BRIEF = """{prefix} Recorded feedback, last at {when}. Sources and quoted targets are explicit below.
An unspecified target must be investigated, never assumed to be the previous assistant reply:
{items}
This is one diagnosis for the whole thread, not one per message. Nothing you write here is sent to him; he reads
the lesson register when he chooses. He does not have to explain the error; working it out is your job. Do this:
1. Establish what happened. Read the conversation around his messages (search_conversations / read_archived_message)
   and, if a background task is involved, its records under {tasks} (checkpoint.json, result.txt, and
   task_state/actions/*.json: one file per tool call). Quote what you actually said or did.
2. State what he expected instead, in his terms, and why the gap happened: the step or rule that produced it.
3. Decide whose fault it is, once for the thread:
   a) Your method: a skill, a habit, a brief, a check you skipped. Find the decision in the method that allowed
      it: the rule, default or omission without which the failure could not have happened. Read the method file
      and the open observations (project_record list kind=lesson) for the other instances it explains. Record it
      with its cause and its edit in one step (remember kind=lesson: content the failure in one line; cause the
      decision that allowed it; anchor the passage quoted exactly from the method, or silent_at where the method
      says nothing; explains the keys of the other observations it accounts for; method_path the skill file, or
      SKILL.md for a habit of conversation; old the anchored text quoted exactly, or empty only where the method is
      silent; new the text in the skill's own voice). The edit removes the cause; a rule that must be remembered
      means the method still permits the failure: skills are not for patching (Alex 2026-10-22). A one-off
      misreading, wording he disliked once, or a rule that restates the specific case is not a lesson: record
      nothing. The runtime shows every edit to Alex on the dashboard and in a weekly message; you do not send it.
   b) The runtime: something the system added, removed, blocked or sent that you do not control (engine, tools,
      the reply pipeline, the dashboard, task bookkeeping). Write {reports}\\<YYYY-MM-DD-HHMM>-<short-slug>.md with:
      his words and time, what happened, the evidence (file paths, task ids, log lines), and the file you suspect.
      Do not propose a skill edit for a runtime fault.
   c) Neither: he disagreed with an idea or changed his mind, and nothing you did was wrong. Record nothing.
4. Your final reply is the record of this diagnosis, at most eight short lines: what went wrong, why, and what you
   registered (the lesson key and the skill file, or the report path, or "nothing"). Ask no question: nobody will
   answer it. Declare task_outcome completed.
"""


def is_diagnosis(request):
    """Record-only runtime work about the agent's own method: a correction diagnosis or a docket's method review
    (tabularium.learning.dockets). Neither may ask the owner anything or claim a completed task."""
    from tabularium.learning.dockets import PREFIX as REVIEW
    return str(request or "").startswith((PREFIX, REVIEW))


def _pending_path(agent):
    return agent.AGENT_DIR / "task_state" / "corrections_pending.json"


def _pending(agent):
    try:
        return json.loads(_pending_path(agent).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def start(agent, chat, message_ids, now=None):
    """Record only explicitly selected inbox messages, with their actual quoted target."""
    if agent.BACKGROUND_TASK_ID or not getattr(agent._background.role, 'learns', False):
        return None
    sources = {m['message_id']: m for m in agent._inbox.turn_sources(chat)}
    if any(type(i) is not int or i not in sources for i in message_ids):
        raise ValueError('Correction must identify an incoming message in this turn.')
    pending = _pending(agent)
    items = pending.setdefault(str(chat), [])
    for ident in dict.fromkeys(message_ids):
        if any(i.get('message_id') == ident for i in items):
            continue
        source = sources[ident]
        ref = source.get('reply_to_message') or {}
        items.append(dict(kind='human', message_id=ident, words=source['text'],
                          reply_to=ref.get('message_id'), before=ref.get('text') or ref.get('caption') or '',
                          at=now or time.time()))
        agent._inbox.record_event('correction', model='owner', role='foreground', details={'message_id': ident})
    atomic_json(_pending_path(agent), pending)
    return len(items)


def burst_note(agent, chat, now=None):
    """The note attached to the next foreground request when Alex has corrected the agent at least twice in the
    current thread and the latest correction is recent (2026-10-23). Tuesday night he corrected Cato eleven times in
    an hour and each reply brought a new fix or a new explanation; what he needed was to be understood first."""
    if agent.BACKGROUND_TASK_ID:
        return ""
    now = now or time.time()
    pending = _pending(agent)
    items = [i for i in pending.get(str(chat), []) if i.get("kind") == "human" and now - i["at"] < QUIET_SECONDS]
    if len(items) < 2 or now - max(i["at"] for i in items) > BURST_RECENT_SECONDS:
        return ""
    # Once per correction: the first evening it fired on every turn within three minutes, so after Alex said
    # "对" to the restatement Cato restated again (21:05:23 and 21:05:58). The note is tied to the latest
    # correction and the turn (inbox job) that first carried it; later turns see it only after a new correction.
    latest = max(i["at"] for i in items)
    job = getattr(getattr(getattr(agent, "_inbox", None), "local", None), "job", None)
    noted = pending.get("noted", {}).get(str(chat)) or {}
    if noted.get("at") == latest and job is not None and noted.get("job") != job:
        return ""
    if job is not None and noted.get("at") != latest:
        pending.setdefault("noted", {})[str(chat)] = {"at": latest, "job": job}
        atomic_json(_pending_path(agent), pending)
    minutes = max(1, int((now - min(i["at"] for i in items)) / 60 + 0.999))
    return BURST_NOTE.format(n=len(items), minutes=minutes)


def start_failure(agent, chat, title, evidence, now=None):
    """A failure recorded by the runtime joins the next diagnosis: a lesson that recurred on a real task, or a declared
    check that failed on a delivered file (2026-10-24, GEPA's point: draft the fix from the actual run). Workers may
    record one; the foreground's minute loop starts the diagnosis. Returns the number waiting."""
    if not getattr(agent._background.role, "learns", False):
        return None
    pending = _pending(agent)
    items = pending.setdefault(str(chat), [])
    items.append({"words": title, "before": evidence[:1500], "at": now or time.time(), "kind": "failure"})
    atomic_json(_pending_path(agent), pending)
    return len(items)


def flush(agent, now=None):
    """Start one diagnosis for each chat whose thread of corrections has gone quiet. Called every minute by the
    engine's worker loop. A diagnosis already active for the chat takes the new corrections as a steer instead.
    Returns the task ids started or steered."""
    now = now or time.time()
    store = agent._background
    pending = _pending(agent)
    started = []
    for chat, items in list(pending.items()):
        if chat == "noted" or not items or now - max(i["at"] for i in items) < QUIET_SECONDS:
            continue
        chat_id = int(chat)
        active = [t for t in store.status(chat_id) if is_diagnosis(t.get("request"))
                  and t.get("status") in ("queued", "starting", "running")]
        if active:
            store.control(chat_id, active[0]["id"], "steer", "Additional recorded feedback (source and target preserved): " + json.dumps(items, ensure_ascii=False))
            started.append(active[0]["id"])
        else:
            reports = agent.AGENT_DIR / "runtime_reports"
            reports.mkdir(exist_ok=True)
            lines = []
            for n, i in enumerate(items, 1):
                if i.get("kind") == "failure":
                    lines.append(f'{n}. [{time.strftime("%H:%M", time.localtime(i["at"]))}] Recorded by the runtime, not Alex\'s words: '
                                 f'{i["words"]}\n   Evidence: {i["before"]}')
                elif i.get('kind') == 'human':
                    lines.append(f'{n}. Alex message {i["message_id"]}: "{i["words"]}"\n'
                                 f'   Quoted target {i["reply_to"]}: "{i["before"]}"' if i.get('reply_to') else
                                 f'{n}. Alex message {i["message_id"]}: "{i["words"]}"\n   No quoted target supplied.')
                else:
                    lines.append(f'{n}. Legacy feedback, source/target unverified; check original records: ' + json.dumps(i, ensure_ascii=False))
            request = BRIEF.format(prefix=PREFIX, when=time.strftime("%Y-%m-%d %H:%M", time.localtime(items[-1]["at"])),
                                   items="\n".join(lines), tasks=store.root, reports=reports)
            task = store.start(chat_id, request, "Full original chat: " + str(agent._conversation_path(chat_id)),
                               extra={"correction": {"words": [i["words"] for i in items], "at": items[-1]["at"]},
                                      "notify_telegram": False})
            started.append(task["id"])
        del pending[chat]
        pending.get("noted", {}).pop(chat, None)
    if started:
        atomic_json(_pending_path(agent), pending)
    return started
