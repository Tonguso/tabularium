"""Memory consolidation with a check (2026-10-20): clean-ups from the weekly review go through the lesson flow.

The weekly review lists duplicates, expired facts and stale sections. Before this the agent proposed them in chat and,
on Alex's yes, rewrote memory.md sentence by sentence through `remember old/new`, and nothing measured whether the
agent still knew what it knew. Now a clean-up is a candidate copy of memory.md that is never live: the agent gathers
the edits (`propose`), the memory exam runs against the candidate and against the live file on the same questions
(`trial`), Alex sees both scores with the diff, and only his go applies it (`apply`). A correction Alex just gave
still lands immediately through `remember old/new`; this path is for tidying, which is where the risk of losing
something lives. One candidate at a time per agent, in task_state/memory_candidate.{md,json}."""
import difflib
import hashlib
import json
import time
from pathlib import Path

from agent_core import memory_exam, trials
from agent_core.memory_store import atomic_text, backup


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _paths(agent_dir):
    state = Path(agent_dir) / "task_state"
    return state / "memory_candidate.md", state / "memory_candidate.json"


def status(agent_dir):
    _, meta = _paths(agent_dir)
    try:
        return json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def propose(agent_dir, old, new, reason=""):
    """Add one exact edit to the candidate. `old` must occur once in the candidate text (or in memory.md for the first
    edit); an empty `new` removes the passage. Returns the running count of edits."""
    agent_dir = Path(agent_dir)
    cand, meta = _paths(agent_dir)
    if not old:
        raise ValueError("A consolidation edit needs the exact current text (old).")
    record = status(agent_dir)
    if record.get("trial"):
        raise ValueError("The candidate has been trialled; apply it, discard it, or wait for Alex's verdict before adding edits.")
    live = agent_dir / "memory.md"
    text = cand.read_text(encoding="utf-8") if cand.exists() else live.read_text(encoding="utf-8-sig")
    if text.count(old) != 1:
        raise ValueError(f"old occurs {text.count(old)} times in the candidate; it must occur exactly once.")
    cand.parent.mkdir(parents=True, exist_ok=True)
    cand.write_text(text.replace(old, new, 1), encoding="utf-8")
    # The full edits and the live file's version are kept: apply refuses if memory.md changed since, and rebase
    # replays these edits on the current file (Codex review 2026-10-20 reproduced a correction lost on apply).
    base = record.get("base") or _digest(live.read_text(encoding="utf-8-sig"))
    edits = record.get("edits", []) + [{"old": old, "new": new, "reason": reason[:200], "at": time.time()}]
    meta.write_text(json.dumps({"edits": edits, "created": record.get("created") or time.time(), "trial": None,
                                "base": base}, ensure_ascii=False, indent=1), encoding="utf-8")
    return len(edits)


def rebase(agent_dir):
    """memory.md changed after the candidate was made (a correction landed): replay the edits on the current file.
    The old trial no longer describes this candidate, so it is cleared and must run again."""
    agent_dir = Path(agent_dir)
    cand, meta = _paths(agent_dir)
    record = status(agent_dir)
    if not record.get("edits"):
        raise ValueError("No candidate to rebase.")
    text = (agent_dir / "memory.md").read_text(encoding="utf-8-sig")
    for n, e in enumerate(record["edits"], 1):
        if text.count(e["old"]) != 1:
            raise ValueError(f"Edit {n} no longer applies to the current memory.md (its text occurs "
                             f"{text.count(e['old'])} times); discard the candidate and propose again.")
        text = text.replace(e["old"], e["new"], 1)
    cand.write_text(text, encoding="utf-8")
    record.update(base=_digest((agent_dir / "memory.md").read_text(encoding="utf-8-sig")), trial=None)
    meta.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    return len(record["edits"])


def diff(agent_dir, limit=4000):
    agent_dir = Path(agent_dir)
    cand, _ = _paths(agent_dir)
    a = (agent_dir / "memory.md").read_text(encoding="utf-8-sig").splitlines()
    b = cand.read_text(encoding="utf-8").splitlines()
    out = "\n".join(difflib.unified_diff(a, b, "memory.md", "candidate", lineterm="", n=1))
    return out[:limit] + ("\n…" if len(out) > limit else "")


def trial(runtime, chat, run=memory_exam.run_exam):
    """The memory exam twice on the same questions: live memory, then the candidate standing in for memory.md
    through the same per-process override that skill trials use. Records both; the model sees scores, not the key."""
    agent_dir = Path(runtime.AGENT_DIR)
    cand, meta = _paths(agent_dir)
    if not cand.exists():
        raise ValueError("No candidate: propose at least one edit first.")
    seed = "memory-candidate-" + str(int(status(agent_dir).get("created") or 0))
    sources = memory_exam.pick_sources(runtime._recall, chat, seed=seed)
    before = run(runtime, chat, sources=sources)
    with trials.local_override(agent_dir / "memory.md", cand):
        after = run(runtime, chat, sources=sources)
    record = status(agent_dir)
    record["trial"] = {"time": time.time(), "before": _score(before), "after": _score(after),
                       "items": [{"question": x["question"], "before": y["score"], "after": x["score"]}
                                 for x, y in zip(after["items"], before["items"])]}
    meta.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    return record["trial"]


def _score(rec):
    return {"correct": rec["correct"], "partial": rec["partial"], "wrong": rec["wrong"], "error": rec.get("error", "")}


def apply(agent_dir):
    """Alex said yes: the candidate becomes memory.md (old text backed up), the candidate is cleared."""
    agent_dir = Path(agent_dir)
    cand, meta = _paths(agent_dir)
    record = status(agent_dir)
    if not cand.exists():
        raise ValueError("No candidate to apply.")
    if not record.get("trial"):
        raise ValueError("Run the trial first so Alex sees the exam before and after.")
    live = agent_dir / "memory.md"
    current = live.read_text(encoding="utf-8-sig")
    if record.get("base") and _digest(current) != record["base"]:
        raise ValueError("memory.md changed after this candidate was made (a correction may have landed). "
                         "Use state=rebase to replay the edits on the current file, then trial again.")
    backup(live, current)
    atomic_text(live, cand.read_text(encoding="utf-8"))
    applied = {"applied": time.time(), "edits": len(record.get("edits", [])), "trial": record["trial"]}
    (agent_dir / "task_state" / "memory_consolidated.json").write_text(json.dumps(applied, ensure_ascii=False, indent=1), encoding="utf-8")
    discard(agent_dir)
    return applied


def discard(agent_dir):
    for p in _paths(agent_dir):
        try:
            p.unlink()
        except OSError:
            pass


def summary(agent_dir):
    """What the agent tells Alex: the edits, and the exam before/after once trialled."""
    record = status(agent_dir)
    if not record:
        return "No memory candidate."
    lines = [f"Memory candidate: {len(record.get('edits', []))} edit(s)."]
    for e in record.get("edits", []):
        lines.append(f"- {e['reason'] or 'edit'}: “{e['old'][:80]}” → “{e['new'][:80] or '(removed)'}”")
    if record.get("base"):
        try:
            if _digest((Path(agent_dir) / "memory.md").read_text(encoding="utf-8-sig")) != record["base"]:
                lines.append("memory.md has changed since: rebase before trial or apply.")
        except OSError:
            pass
    t = record.get("trial")
    if t:
        b, a = t["before"], t["after"]
        lines.append(f"Memory exam on the same questions: live {b['correct']}/{b['partial']}/{b['wrong']} "
                     f"(correct/partial/wrong) → candidate {a['correct']}/{a['partial']}/{a['wrong']}."
                     + (f" Exam error: {a['error'] or b['error']}" if a["error"] or b["error"] else ""))
        lines.append("Awaiting Alex's go to apply.")
    else:
        lines.append("Not yet trialled: run the trial before showing it to Alex.")
    return "\n".join(lines)
