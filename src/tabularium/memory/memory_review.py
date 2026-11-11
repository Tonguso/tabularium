"""Weekly memory review: a report of clean-up candidates in memory.md, for the agent to propose to Alex (2026-10-11).

Nothing here changes memory. The report lists what a reader would want to merge or retire; the agent reads it,
proposes the edits to Alex in one message, and changes nothing until he agrees (the same rule as approved lessons).

Candidates:
- duplicates: two bullet lines that say nearly the same thing (character-bigram Jaccard >= DUP_THRESHOLD), outside the
  historical sections, where repetition is expected;
- expired facts: sourced facts whose valid_to has passed but which still sit in the current section;
- stale ACTIVE sections: a heading marked ACTIVE whose text carries no date in the last STALE_ACTIVE_DAYS days;
- core size: the lines above the detail marker, which every turn pays for;
- changed facts (2026-11-11, after Anthropic's note on agent memory): every current sourced fact is read against
  Alex's later messages, and the examiner model says whether he changed it. The record outranks the note. The
  memory file had carried two "current" email methods for two days, one of them superseded by his own message.
Output: task_state/memory_review.md (the report) and task_state/memory_review.json (counts, for the dashboard and
for the one-time note the agent receives on his next turn)."""
import json
import re
from tabularium.memory.memory_store import current_facts
import time
from datetime import datetime, timedelta
from pathlib import Path

DUP_THRESHOLD = 0.6
MAX_FACT_CHECKS = 60
FACT = re.compile(r"(?ms)^### Fact ([0-9a-f]+)\n```json\n(.*?)\n```")
CHANGE_PROMPT = """A fact recorded about Alex, and messages he sent later that share words with it.

Recorded from his message of {said_at}: "{quote}"
Fact: {content}

Later messages from Alex:
{later}

Did a later message change, revoke or replace what the fact records (a new decision, a changed preference, a method he
swapped for another)? A message that repeats the fact, applies it, or talks about the same subject without changing it is
not a change. Answer changed=true only where his own words say so, with that message's id and the exact words."""
CHANGE_SCHEMA = {"type": "object", "properties": {"changed": {"type": "boolean"}, "message_id": {"type": "integer"},
                                                  "quote": {"type": "string"}, "note": {"type": "string"}},
                 "required": ["changed", "message_id", "quote", "note"], "additionalProperties": False}
STALE_ACTIVE_DAYS = 45
MAX_DUPLICATES = 20
MIN_BULLET = 30
SKIP_SECTIONS = ("Activity Archive", "Parked Threads", "Superseded facts", "Recent Activity", "Open Threads", "Sourced facts")
MARKER = "<!-- MEMORY-DETAIL-BELOW"
DATE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")


def _grams(text):
    s = re.sub(r"\s+", "", text.lower())
    return {s[i:i + 2] for i in range(len(s) - 1)}


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def sections(text):
    """[(heading, level, body_lines, first_line_number)] for every #, ## or ### heading; the preamble is ('', 0, ...)."""
    out = []
    heading, level, body, start = "", 0, [], 1
    for number, line in enumerate(text.splitlines(), 1):
        m = re.match(r"^(#{1,3})\s+(.*)", line)
        if m:
            out.append((heading, level, body, start))
            heading, level, body, start = m.group(2).strip(), len(m.group(1)), [], number
        else:
            body.append(line)
    out.append((heading, level, body, start))
    return out


def _skipped(heading):
    return any(heading.startswith(name) for name in SKIP_SECTIONS)


def duplicates(text, threshold=DUP_THRESHOLD, limit=MAX_DUPLICATES):
    bullets = []
    for heading, _, body, start in sections(text):
        if _skipped(heading):
            continue
        for offset, line in enumerate(body, 1):
            stripped = line.strip()
            if stripped.startswith("- ") and len(stripped) >= MIN_BULLET:
                bullets.append((stripped, heading, start + offset, _grams(stripped)))
    found = []
    for i in range(len(bullets)):
        a = bullets[i]
        for j in range(i + 1, len(bullets)):
            b = bullets[j]
            if abs(len(a[0]) - len(b[0])) > max(len(a[0]), len(b[0])) * 0.5:
                continue
            score = jaccard(a[3], b[3])
            if score >= threshold:
                found.append({"score": round(score, 2), "a": a[0][:200], "a_section": a[1], "a_line": a[2],
                              "b": b[0][:200], "b_section": b[1], "b_line": b[2]})
    found.sort(key=lambda d: -d["score"])
    return found[:limit]


def expired_facts(text, today):
    current = current_facts(text)
    out = []
    for m in re.finditer(r"(?ms)^### Fact ([0-9a-f]+)\n```json\n(.*?)\n```", current):
        try:
            record = json.loads(m.group(2))
        except ValueError:
            continue
        if record.get("valid_to") and record["valid_to"] < today:
            out.append({"id": m.group(1), "content": str(record.get("content", ""))[:200], "valid_to": record["valid_to"]})
    return out


def stale_active(text, today, days=STALE_ACTIVE_DAYS):
    cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")
    out = []
    for heading, _, body, start in sections(text):
        if "ACTIVE" not in heading.upper() or _skipped(heading):
            continue
        dates = ["-".join(m) for m in DATE.findall(heading + "\n" + "\n".join(body))]
        newest = max(dates) if dates else ""
        if newest < cutoff:
            out.append({"heading": heading[:120], "line": start, "newest_date": newest or "none", "lines": len(body)})
    return out


def sourced_facts(text):
    """Current facts that came from one of Alex's messages: id, content, the source message id, when, his words."""
    out = []
    for m in FACT.finditer(current_facts(text)):
        try:
            record = json.loads(m.group(2))
        except ValueError:
            continue
        evidence = record.get("evidence") or {}
        source = str(evidence.get("source") or record.get("source") or "")
        if not source.startswith("message:"):
            continue
        try:
            ident = int(source.split(":", 1)[1])
        except ValueError:
            continue
        out.append({"id": m.group(1), "content": str(record.get("content", ""))[:600], "source_id": ident,
                    "said_at": str(evidence.get("said_at") or "")[:19], "quote": str(evidence.get("quote") or "")[:300]})
    return out


def changed_facts(text, chat, recall, examiner, limit=MAX_FACT_CHECKS):
    """Each current sourced fact against Alex's later messages on the same words. The examiner reads them and says
    whether he changed the fact; its verdict must name one of the messages shown. Stops at the first examiner
    failure and reports it, so a model outage never reads as "nothing changed"."""
    out, error = [], ""
    for fact in sourced_facts(text)[:limit]:
        try:
            found = recall.search(chat, query=fact["content"], speaker="user", date_from=fact["said_at"][:10], limit=5)
        except ValueError:
            continue
        later = [m for m in found.get("matches", []) if int(m.get("id") or 0) > fact["source_id"]]
        if not later:
            continue
        shown = "\n".join(f"- message {m['id']} ({str(m.get('said_at', ''))[:16]}): {str(m.get('excerpt', ''))[:500]}" for m in later)
        try:
            verdict = json.loads(examiner(CHANGE_PROMPT.format(said_at=fact["said_at"][:10], quote=fact["quote"],
                                                               content=fact["content"], later=shown), CHANGE_SCHEMA) or "")
        except ValueError:
            continue
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:200]
            break
        if verdict.get("changed") and verdict.get("message_id") in {int(m["id"]) for m in later}:
            out.append({"id": fact["id"], "content": fact["content"][:200], "source_id": fact["source_id"],
                        "said_at": fact["said_at"][:10], "later_id": int(verdict["message_id"]),
                        "quote": str(verdict.get("quote", ""))[:300], "note": str(verdict.get("note", ""))[:200]})
    return out, error


def core_size(text):
    head = text.split(MARKER)[0] if MARKER in text else text
    return len(head.splitlines())


def review(text, today=None, chat=None, recall=None, examiner=None):
    today = today or datetime.now().strftime("%Y-%m-%d")
    dups = duplicates(text)
    facts = expired_facts(text, today)
    stale = stale_active(text, today)
    changed, error = changed_facts(text, chat, recall, examiner) if recall is not None and examiner is not None else ([], "")
    return {"time": time.time(), "today": today, "duplicates": dups, "expired_facts": facts, "stale_active": stale,
            "changed_facts": changed, "error": error,
            "core_lines": core_size(text), "total_lines": len(text.splitlines()),
            "candidates": len(dups) + len(facts) + len(stale) + len(changed), "shown_at": None}


def report_markdown(result, path_hint=""):
    lines = [f"# Memory review {result['today']}", "",
             f"{result['candidates']} candidate clean-up(s). Propose them to Alex; change nothing until he agrees.",
             f"Core (above the detail marker): {result['core_lines']} lines of {result['total_lines']}.", ""]
    if result["duplicates"]:
        lines += ["## Near-duplicate bullets (merge or drop one)", ""]
        for d in result["duplicates"]:
            lines += [f"- {d['score']:.2f}  line {d['a_line']} ({d['a_section']}): {d['a']}",
                      f"        line {d['b_line']} ({d['b_section']}): {d['b']}"]
        lines.append("")
    if result["expired_facts"]:
        lines += ["## Expired sourced facts (valid_to passed; supersede or move to the historical section)", ""]
        lines += [f"- Fact {f['id']} (valid to {f['valid_to']}): {f['content']}" for f in result["expired_facts"]]
        lines.append("")
    if result["stale_active"]:
        lines += [f"## Sections marked ACTIVE with no date in the last {STALE_ACTIVE_DAYS} days (still active, or retire?)", ""]
        lines += [f"- line {s['line']}: {s['heading']} (newest date {s['newest_date']}, {s['lines']} lines)" for s in result["stale_active"]]
        lines.append("")
    if result.get("changed_facts"):
        lines += ["## Facts his later messages changed (supersede each on his word: remember with supersedes=<fact id>)", ""]
        for f in result["changed_facts"]:
            lines += [f"- Fact {f['id']} ({f['said_at']}): {f['content']}",
                      f"        message {f['later_id']}: \"{f['quote']}\" {f['note']}".rstrip()]
        lines.append("")
    if result.get("error"):
        lines += [f"The check of sourced facts against later messages stopped early: {result['error']}", ""]
    if not result["candidates"]:
        lines.append("Nothing to propose this week.")
    return "\n".join(lines) + "\n"


def run(agent_dir, today=None, chat=None, recall=None, examiner=None):
    """Review <agent_dir>/memory.md and write the report and the json summary under task_state. Returns the summary.
    With `chat`, `recall` and `examiner`, the sourced facts are also read against Alex's later messages."""
    agent_dir = Path(agent_dir)
    memory = agent_dir / "memory.md"
    text = memory.read_text(encoding="utf-8-sig") if memory.exists() else ""
    result = review(text, today, chat=chat, recall=recall, examiner=examiner)
    state = agent_dir / "task_state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "memory_review.md").write_text(report_markdown(result), encoding="utf-8")
    (state / "memory_review.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result
