"""Weekly memory review: a report of clean-up candidates in memory.md, for the agent to propose to Alex (2026-10-11).

Nothing here changes memory. The report lists what a reader would want to merge or retire; the agent reads it,
proposes the edits to Alex in one message, and changes nothing until he agrees (the same rule as approved lessons).

Candidates:
- duplicates: two bullet lines that say nearly the same thing (character-bigram Jaccard >= DUP_THRESHOLD), outside the
  historical sections, where repetition is expected;
- expired facts: sourced facts whose valid_to has passed but which still sit in the current section;
- stale ACTIVE sections: a heading marked ACTIVE whose text carries no date in the last STALE_ACTIVE_DAYS days;
- core size: the lines above the detail marker, which every turn pays for.
Output: task_state/memory_review.md (the report) and task_state/memory_review.json (counts, for the dashboard and
for the one-time note the agent receives on his next turn)."""
import json
import re
from tabularium.memory.memory_store import current_facts
import time
from datetime import datetime, timedelta
from pathlib import Path

DUP_THRESHOLD = 0.6
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


def core_size(text):
    head = text.split(MARKER)[0] if MARKER in text else text
    return len(head.splitlines())


def review(text, today=None):
    today = today or datetime.now().strftime("%Y-%m-%d")
    dups = duplicates(text)
    facts = expired_facts(text, today)
    stale = stale_active(text, today)
    return {"time": time.time(), "today": today, "duplicates": dups, "expired_facts": facts, "stale_active": stale,
            "core_lines": core_size(text), "total_lines": len(text.splitlines()),
            "candidates": len(dups) + len(facts) + len(stale), "shown_at": None}


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
    if not result["candidates"]:
        lines.append("Nothing to propose this week.")
    return "\n".join(lines) + "\n"


def run(agent_dir, today=None):
    """Review <agent_dir>/memory.md and write the report and the json summary under task_state. Returns the summary."""
    agent_dir = Path(agent_dir)
    memory = agent_dir / "memory.md"
    text = memory.read_text(encoding="utf-8-sig") if memory.exists() else ""
    result = review(text, today)
    state = agent_dir / "task_state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "memory_review.md").write_text(report_markdown(result), encoding="utf-8")
    (state / "memory_review.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result
