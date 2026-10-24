"""When Alex corrects an agent, the agent works out the specifics, not Alex (2026-10-20; redesigned 2026-10-22).

Alex: "Agents only learn lessons when there is specific instruction on errors and fixes. They don't learn lessons if
I'm angry but not telling them specifics." So the reply envelope says whether his message corrected the agent
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
the lesson register, and he reads the digest when he wants to (python agent_control.py lessons; or asks the agent)."""
import json
import time

from agent_runtime import atomic_json

PREFIX = "[Correction diagnosis]"
QUIET_SECONDS = 600            # a thread is over when Alex has not corrected for this long
BURST_RECENT_SECONDS = 180     # a burst note applies to replies within this long of the latest correction

BURST_NOTE = ("[Runtime note: Alex has corrected you {n} times in the last {minutes} minutes. Do not produce, fix, compute, "
              "propose or explain anything in this reply. In two or three lines, using the words he has used, say what you "
              "now understand he means and wants, then ask whether that is right. Nothing else.]")

BRIEF = """{prefix} Alex corrected you {count}, the last at {when}. In order, his words and the reply of yours each
was reacting to:
{items}
This is one diagnosis for the whole thread, not one per message. Nothing you write here is sent to him; he reads
the lesson register when he chooses. He does not have to explain the error; working it out is your job. Do this:
1. Establish what happened. Read the conversation around his messages (search_conversations / read_archived_message)
   and, if a background task is involved, its records under {tasks} (checkpoint.json, result.txt, and
   task_state/actions/*.json: one file per tool call). Quote what you actually said or did.
2. State what he expected instead, in his terms, and why the gap happened: the step or rule that produced it.
3. Decide whose fault it is, once for the thread:
   a) Your method: a skill, a habit, a brief, a check you skipped. Record it (remember kind=lesson, failure and the
      correction in one line). Register an exact edit with lesson(action=propose) only if the same fault would recur
      without it and one sharpened rule removes it: the skill file, the current text quoted exactly, the new text in
      the skill's own voice. Skills are not for patching (Alex 2026-10-22): a one-off misreading, wording he
      disliked once, or a rule that restates the specific case stays a candidate with no edit.
   b) The runtime: something the system added, removed, blocked or sent that you do not control (engine, tools,
      the reply pipeline, the dashboard, task bookkeeping). Write {reports}\\<YYYY-MM-DD-HHMM>-<short-slug>.md with:
      his words and time, what happened, the evidence (file paths, task ids, log lines), and the file you suspect.
      Do not propose a skill edit for a runtime fault.
   c) Neither: he disagreed with an idea or changed his mind, and nothing you did was wrong. Record nothing.
4. Your final reply is the record of this diagnosis, at most eight short lines: what went wrong, why, and what you
   registered (the lesson key and the skill file, or the report path, or "nothing"). Ask no question: nobody will
   answer it. Declare task_outcome completed.
"""


def _text(content):
    if isinstance(content, str):
        return content
    return " ".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


def is_diagnosis(request):
    return str(request or "").startswith(PREFIX)


def _pending_path(agent):
    return agent.AGENT_DIR / "task_state" / "corrections_pending.json"


def _pending(agent):
    try:
        return json.loads(_pending_path(agent).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def start(agent, chat, messages, now=None):
    """Record the correction in the latest exchange for the next diagnosis of this chat's thread. Returns the number
    of corrections now waiting, or None when the agent does not learn this way (Tacitus) or runs as a worker."""
    store = agent._background
    if agent.BACKGROUND_TASK_ID or not getattr(store.role, "learns", False):
        return None
    users = [m for m in messages if m.get("role") == "user" and _text(m.get("content")).strip()]
    words = _text(users[-1]["content"]).strip()[:1500] if users else ""
    before = ""
    for m in reversed(messages[:-1] if messages and messages[-1].get("role") == "assistant" else messages):
        if m.get("role") == "assistant" and _text(m.get("content")).strip():
            before = _text(m["content"]).strip()[:1500]
            break
    inbox = getattr(agent, "_inbox", None)
    if inbox is not None and hasattr(inbox, "record_event"):
        inbox.record_event("correction", model="owner", role="foreground")
    pending = _pending(agent)
    items = pending.setdefault(str(chat), [])
    items.append({"words": words, "before": before, "at": now or time.time()})
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
    items = [i for i in pending.get(str(chat), []) if now - i["at"] < QUIET_SECONDS]
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
            store.control(chat_id, active[0]["id"], "steer", "Alex added, while you were diagnosing: " +
                          " / ".join('"' + i["words"] + '"' for i in items))
            started.append(active[0]["id"])
        else:
            reports = agent.AGENT_DIR / "runtime_reports"
            reports.mkdir(exist_ok=True)
            lines = []
            for n, i in enumerate(items, 1):
                lines.append(f'{n}. [{time.strftime("%H:%M", time.localtime(i["at"]))}] "{i["words"]}"\n'
                             f'   You had said: "{i["before"]}"')
            count = "once" if len(items) == 1 else f"{len(items)} times"
            request = BRIEF.format(prefix=PREFIX, count=count, when=time.strftime("%Y-%m-%d %H:%M", time.localtime(items[-1]["at"])),
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
