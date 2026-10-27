"""The reply is a declared field, on every model route (2026-10-12).

A turn used to end with "whatever text the model produced last", and that text was sent to Alex. At 19:27 on
2026-10-12 Cato's last text was his working notes, in English, and Alex received them. Nothing in the design said
which text was the message; any check added afterwards would have been a guess about the text.

Now every route asks the model for a structured final answer with one field, `reply`, using each provider's native
structured output (Claude subscription: output_format; Anthropic API: output_config; OpenAI Responses: text.format).
Only that field is delivered and saved as the assistant's message. Text outside it is never sent. A turn that ends
without a valid field stops with an error rather than sending something else.

2026-10-15: the answer also declares its `modality`. Until then a voice reply began with a [[VOICE]] tag that the
runtime regex-stripped from the reply, from history and from every outgoing message. The delivered text is a Reply,
a str that carries the modality and whether the turn completed (False when the runtime ended it: step limit, cut-off).
"""
import json

SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {
            "type": "string",
            "description": ("The complete message Alex will read, and nothing else: no notes to yourself, no "
                            "working, no commentary about tools or checks. Follow the conversation's language "
                            "and formatting rules. Only this field is delivered."),
        },
        "modality": {
            "type": "string",
            "enum": ["text", "voice"],
            "description": ("text, unless this turn answers a voice message with something that works heard aloud; "
                            "then voice. Anything better seen than heard (code, tables, lists, figures, links) is text."),
        },
        "work": {
            "type": "string",
            "enum": ["none", "started", "awaiting_go"],
            "description": ("What this reply does about further work. started: a background task was created in this "
                            "turn. awaiting_go: you propose work and ask Alex to confirm before starting it. none: "
                            "no work is promised. Never write that you will do something unless this is started or "
                            "awaiting_go."),
        },
    },
    "required": ["reply", "modality", "work", "task_outcome", "question", "decision_task_id", "status_claims", "correction",
                 "answers", "request"],
    "additionalProperties": False,
}

SCHEMA["properties"].update({
    "task_outcome": {"type": "string", "enum": ["none", "completed", "waiting", "blocked"],
                     "description": "Background task outcome, not whether this message is finished. waiting means a decision is needed; completed means the entire requested scope succeeded. Foreground: none."},
    "decision_task_id": {"type": "integer", "description": "Task this question belongs to; 0 for a general conversation decision."},
    "question": {"type": "string", "description": "The exact unresolved decision and options, if any. Empty otherwise. For a foreground question tied to a task, first persist it with task_decision ask."},
    "status_claims": {"type": "array", "items": {"type": "object", "properties": {
        "task_id": {"type": "integer"},
        "state": {"type": "string", "enum": ["running", "queued", "waiting", "finished", "stopped"]}},
        "required": ["task_id", "state"], "additionalProperties": False},
        "description": "EVERY claim in reply about this agent's task progress, including implicit claims such as 'still working'. Identify the task; live evidence will be checked before delivery. Empty only when there are no status claims."},
    "correction": {"type": "array", "items": {"type": "integer"},
                   "description": "IDs from the Incoming messages list that correct you or the system. Empty if none. Runtime warnings are not human feedback. Select each actual correction, including follow-ups; disagreement or a changed preference alone is not a correction."},
    "answers": {"type": "array", "items": {"type": "integer"},
                "description": ("IDs from Incoming messages that this reply answers or acknowledges. One reply can "
                                "cover several messages: when a correction changes an earlier request and you handle "
                                "the revised request, include BOTH IDs. Include unrelated requests only if also "
                                "addressed. Use exact current-turn IDs, never copied text or invented IDs. "
                                "This declares message coverage, not proof that a deliverable passed its checks. "
                                "Empty when there are no incoming message IDs.")},
    "request": {"type": "string", "enum": ["done", "started", "asking", "blocked", "none"],
                "description": ("Where Alex's request stands after this reply. done: delivered in this reply. started: "
                                "background work was started this turn. asking: you need his answer to continue, and "
                                "the reply asks it. blocked: not done, nothing running, nothing asked, for example a "
                                "tool refused; say so honestly, and the runtime gives you one more try to take another "
                                "route or ask him. none: he made no request (conversation, opinion).")},
})

# Provider-specific request parameters, all built from the one schema.
CLAUDE_SDK_OUTPUT_FORMAT = {"type": "json_schema", "schema": SCHEMA}
ANTHROPIC_OUTPUT_CONFIG = {"format": {"type": "json_schema", "schema": SCHEMA}}
OPENAI_TEXT = {"format": {"type": "json_schema", "name": "reply", "schema": SCHEMA, "strict": True}}


class ReplyMissing(RuntimeError):
    """The model ended its turn without a usable reply field. Nothing is sent."""


WORK = ("none", "started", "awaiting_go")


class Reply(str):
    """The delivered message. modality: 'text' or 'voice'. work: what it claims about further work (WORK).
    complete: False when the runtime ended the turn itself."""
    modality = "text"
    work = "none"
    complete = True
    decision_id = 0          # the decision this message asks; send_message records its Telegram id on it (2026-10-21)
    answers = ()             # exact current-turn message IDs covered by this reply
    request = "none"         # where his request stands: done, started, asking, blocked, none (2026-10-21)

    def __new__(cls, text, modality="text", complete=True, work="none", task_outcome="none", question="", decision_task_id=0, status_claims=None, correction=(), answers=(), request="none"):
        self = super().__new__(cls, text)
        self.answers = tuple(a for a in answers if type(a) is int and a > 0) if isinstance(answers, (list, tuple)) else ()
        self.request = request if request in ("done", "started", "asking", "blocked", "none") else "none"
        self.correction = tuple(correction) if isinstance(correction, (list, tuple)) and all(type(i) is int for i in correction) else ()
        self.modality = modality if modality in ("text", "voice") else "text"
        self.work = work if work in WORK else "none"
        self.complete = complete
        self.task_outcome = task_outcome
        self.decision_task_id = decision_task_id
        self.question = question
        self.status_claims = status_claims or []
        return self


def retry_note(agent, reason, draft, note):
    """Runtime feedback belongs to this exact rejected draft, not to a human message."""
    agent._inbox.record_event('bounce', reason, role='runtime', details={'draft': str(draft), 'warning': note})
    return {'role': 'user', 'content': 'Runtime feedback on your undelivered draft:\n' + str(draft) + '\n' + note}


UNBACKED_PROMISE = ("[System: your reply declares work=started, but no background task was created in this turn. A "
                    "promise with nothing behind it is not delivered. Either start the work now with background_task, "
                    "or set work=awaiting_go and ask Alex to confirm.]")


def answered(reply, turn_messages):
    """Resolve declared coverage against this turn's message identities; never infer it from text."""
    ids = set(getattr(reply, "answers", ()))
    return [(mid, text) for mid, text in turn_messages if mid in ids]


def unanswered(reply, turn_messages):
    """Messages the turn took in that the reply does not cover. Only checked when the turn took in more than one:
    2026-10-21 17:44-17:45 Alex sent three, Cato's reply covered the last and dropped two."""
    if not isinstance(turn_messages, list) or len(turn_messages) < 2:
        return []
    covered = {mid for mid, _ in answered(reply, turn_messages)}
    return [(mid, text) for mid, text in turn_messages if mid not in covered]


def unanswered_note(missing):
    lines = "\n".join(f"- ID {mid}: {str(text or '')[:80]}" for mid, text in missing)
    return ("[System: your reply does not cover these incoming message IDs:\n" + lines +
            "\nAddress each, or acknowledge how it is being handled, and include its ID in answers. "
            "One response to a corrected request can cover both the original and correction IDs. "
            "Do not claim coverage of unrelated requests you have not addressed. Nothing has been sent yet.]")


DEAD_END = ("[System: your reply says Alex's request is blocked: not done, nothing running, nothing asked. That "
            "leaves him waiting for something that is not happening (2026-10-21: a logo request stopped silently after "
            "the image service refused it). Take another route now (the loaded skill's own method, a different tool), "
            "start background work, or ask him the one thing you need. If none is possible, say plainly that you have "
            "stopped and why. Nothing has been sent yet.]")


def dead_end(reply) -> bool:
    """True when the reply declares the request blocked: sent back once for another route or a question."""
    return getattr(reply, "request", "none") == "blocked"


def unbacked(reply, started) -> bool:
    """True when a reply claims work was started and the turn created no task (2026-10-16: Cal wrote "I will narrow
    the research to..." at 15:26 and started nothing; Alex waited an hour and three quarters)."""
    return getattr(reply, "work", "none") == "started" and not started


def reply(value) -> Reply:
    """The reply from a structured final answer (a dict, or the JSON text of one). Raises ReplyMissing."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise ReplyMissing("The model's final answer was not in the declared reply format; nothing was sent.")
    text = value.get("reply") if isinstance(value, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise ReplyMissing("The model finished without a reply; nothing was sent.")
    return Reply(text.strip(), value.get("modality", "text"), work=value.get("work", "none"),
                 task_outcome=value.get("task_outcome", "none"), question=value.get("question", ""),
                 decision_task_id=value.get("decision_task_id", 0), status_claims=value.get("status_claims", []),
                 correction=value.get("correction", []), answers=value.get("answers") or (),
                 request=value.get("request", "none"))


def readable(text: str) -> str:
    """For history only: the reply inside an envelope, or the text unchanged when it is not one."""
    try:
        return reply(text)
    except ReplyMissing:
        return text
