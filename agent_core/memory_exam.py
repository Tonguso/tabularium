"""Weekly memory exam: does the agent remember what Alex told him this week? (2026-10-11)

The daily self-test proves the agent answers; it says nothing about memory. This picks a few of Alex's own messages
from the past EXAM_DAYS days out of the conversation record, has an examiner model turn each into a question in
different words with the expected answer, asks the agent through his normal turn (read_memory allowed, the
conversation archive not), and has the examiner grade the answer against the expectation. The result goes to
task_state/memory_exam.json; the dashboard shows it and the supervisor tells Alex when it fails.

The examiner is the cheapest Anthropic model on the paid API, the route the advisor already uses. The agent's own
answer runs on his usual route. Nothing is sent to Telegram and nothing is written to memory."""
import json
import random
import re
import time
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

EXAM_DAYS = 7
EXAM_ITEMS = 3            # questions asked
EXAM_POOL = 8             # candidate messages sampled; the examiner skips the ones with nothing to remember
MIN_CHARS, MAX_CHARS = 40, 600
EXAMINER_ALIAS = "haiku"
EXAM_TOOLS = ("read_memory",)   # the only tool that exists during the exam; the conversation archive does not

EXAMINER_SYSTEM = ("You write and grade short memory tests for a personal assistant. Alex is the assistant's owner. "
                   "Answer only with the JSON asked for, nothing else.")

QUESTION_PROMPT = """Alex wrote this to his assistant on {said_at}:

<message>
{text}
</message>

If the message states something durable the assistant should remember (a fact, a decision, a preference, a plan, a name,
a number), write ONE question that tests it, phrased in different words from the message, in the same language as the
message, without quoting the message. The question must stand on its own: name the subject explicitly (a file, a
person, a project, a date), never "this", "these", "it" or "the two files" (the first run asked "what are these two
files for" with no file named). Give the expected answer in a few words.
Durable means it should still be true next month: a standing preference, a rule for how work is done, a fact about
Alex or his projects, a decision. Skip instructions about the content or wording of one deliverable (what a slide,
section or document should say), one-off requests, and anything only meaningful inside that task: those live in the
task record, not in memory (the 2026-10-19 exam asked about a PPT's section structure and graded "not in memory" wrong).
If the message is a question, a command, chit-chat, or contains nothing worth remembering, answer {{"skip": true}}.

Fill question and expected, with skip false; or skip true with both empty."""

QUESTION_SCHEMA = {"type": "object", "properties": {"question": {"type": "string"}, "expected": {"type": "string"},
                                                    "skip": {"type": "boolean"}},
                   "required": ["question", "expected", "skip"], "additionalProperties": False}

GRADE_PROMPT = """Question: {question}
Expected answer (ground truth): {expected}
Assistant's answer: {answer}

Grade the assistant's answer against the expected answer: "correct" if it contains the expected fact (wording may differ),
"partial" if it is incomplete or hedged but not wrong, "wrong" if it contradicts the expected answer or says it does not know.
note: one short sentence."""

GRADE_SCHEMA = {"type": "object", "properties": {"score": {"type": "string", "enum": ["correct", "partial", "wrong"]},
                                                 "note": {"type": "string"}},
                "required": ["score", "note"], "additionalProperties": False}

ASK_PREFIX = ("[Memory exam: a weekly check run by the runtime, not a message from Alex. Answer from what you remember; "
              "read_memory is the only tool available. If you do not remember, say so plainly.]\n")


def _json(text):
    try:
        return json.loads(text or "")
    except ValueError:
        return {}


def pick_sources(recall, chat, days=EXAM_DAYS, n=EXAM_POOL, seed=None, now=None):
    """Alex's own messages from the last `days` days that could carry something to remember: statements, not
    commands or questions, of a sensible length. A random sample of n, seeded by the week so a rerun asks the same."""
    now = now or datetime.now()
    since = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    with closing(recall.connect()) as db:
        rows = db.execute("SELECT id, text, said_at FROM messages WHERE chat=? AND speaker='user' AND said_at>=? ORDER BY id",
                          (str(chat), since)).fetchall()
    good = []
    for r in rows:
        text = (r["text"] or "").strip()
        if not MIN_CHARS <= len(text) <= MAX_CHARS or text.startswith("/"):
            continue
        if text.rstrip().endswith(("?", "？")):
            continue
        good.append({"id": r["id"], "text": text, "said_at": r["said_at"]})
    rng = random.Random(seed if seed is not None else now.strftime("%Y-%W"))
    return rng.sample(good, n) if len(good) > n else good


def examiner_call(runtime, prompt, schema):
    """One examiner call with the answer requested as structured output in `schema`."""
    model = runtime.ANTHROPIC_MODEL_ALIASES[EXAMINER_ALIAS]
    resp = runtime.anthropic_client.with_options(timeout=60.0, max_retries=1).messages.create(
        model=model, max_tokens=400, system=EXAMINER_SYSTEM, messages=[{"role": "user", "content": prompt}],
        output_config={"format": {"type": "json_schema", "schema": schema}})
    try:
        runtime._inbox.record_event("response", model, "api", "anthropic", getattr(resp, "usage", None), role="examiner")
    except Exception:
        pass
    return "\n".join(b.text for b in resp.content if getattr(b, "type", None) == "text")


def ask_agent(runtime, chat, question):
    """One turn under the conditions of a real conversation: the fixed seed, a timestamped message, and the current core
    memory attached by the request builder, as every turn gets it. Only EXAM_TOOLS exist, enforced by tool_scope.
    A fresh message list, so the live conversation is untouched. Returns (answer, tools_used)."""
    messages = runtime.load_memory_context() + [{"role": "user", "content": f"{runtime._ts()} {ASK_PREFIX}Question: {question}"}]
    with runtime.tool_scope(EXAM_TOOLS):
        answer = runtime.run_turn(messages, chat) or ""
    used = [b.get("name") for m in messages if m.get("role") == "assistant" and isinstance(m.get("content"), list)
            for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_use"]
    return answer, used


def run_exam(runtime, chat, examiner=None, ask=None, sources=None, now=None):
    """The whole exam under the chat lock. examiner/ask/sources are injectable for tests."""
    examiner = examiner or (lambda prompt, schema: examiner_call(runtime, prompt, schema))
    ask = ask or (lambda question: ask_agent(runtime, chat, question))
    started = time.time()
    record = {"time": started, "items": [], "correct": 0, "partial": 0, "wrong": 0, "skipped": 0, "ok": True, "error": ""}
    try:
        picked = sources if sources is not None else pick_sources(runtime._recall, chat, now=now)
        for src in picked:
            if len(record["items"]) >= EXAM_ITEMS:
                break
            q = _json(examiner(QUESTION_PROMPT.format(said_at=src["said_at"], text=src["text"]), QUESTION_SCHEMA))
            if q.get("skip") or not q.get("question") or not q.get("expected"):
                record["skipped"] += 1
                continue
            answer, used = ask(q["question"])
            g = _json(examiner(GRADE_PROMPT.format(question=q["question"], expected=q["expected"], answer=answer[:2000]), GRADE_SCHEMA))
            score = g.get("score") if g.get("score") in ("correct", "partial", "wrong") else "wrong"
            record["items"].append({"source_id": src["id"], "said_at": src["said_at"], "question": q["question"],
                                    "expected": q["expected"], "answer": answer[:400], "score": score,
                                    "note": str(g.get("note", ""))[:200], "tools": used})
            record[score] += 1
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    record["ok"] = not record["error"] and record["wrong"] == 0
    record["seconds"] = round(time.time() - started, 1)
    return record


def save(agent_dir, record):
    state = Path(agent_dir) / "task_state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "memory_exam.json").write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")


def summary(record):
    n = len(record["items"])
    if record.get("error"):
        return "error: " + record["error"]
    if not n:
        return "nothing to examine this week"
    return f"{record['correct']}/{n} correct, {record['partial']} partial, {record['wrong']} wrong"
