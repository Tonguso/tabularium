"""Research-quality signal: are the agent's answers to real past tasks drifting? (2026-10-24, Alex: [quote removed])

The regression eval (tabularium.learning.regression) decides pass or fail from declared checks on synthetic cases, and it is
right to: a model grading a model's prose passed a wrong file. But nothing then tracks the work Alex cares about,
research with no deterministic answer. This module keeps a labelled signal, never a score: a few of the collected
real cases (<agent>/evals/cases.json, the "candidates" the eval does not grade) are rerun in isolation, and a cheap
model says only whether the new answer is closer to what was asked than the earlier accepted answer, about the same,
or further. Three weeks of "further" on research tasks means something changed; one run means little.

    python agent_control.py eval cato --signal 3

Reruns carry an eval context without a benchmark fingerprint: isolated (nothing sent, delegated or started) but
with the agent's normal tools, so a research rerun can search. Results: <agent>/task_state/research_signal/<stamp>.json.
"""
import json
import re
import time
import uuid
from pathlib import Path

from tabularium.learning import regression
CLOSENESS = ("closer", "same", "further")
# The judge says which answer serves the request better; the code maps it (new -> closer, earlier -> further).
# The first run's judge, asked for "closer/further" directly, inverted one verdict (2026-10-24).
BETTER = {"new": "closer", "equal": "same", "earlier": "further"}
SIGNAL_SCHEMA = {"type": "object", "properties": {
    "better": {"type": "string", "enum": ["new", "equal", "earlier"]},
    "reason": {"type": "string"}},
    "required": ["better", "reason"], "additionalProperties": False}
SIGNAL_PROMPT = """Task request:
{request}

Earlier answer, the one that was accepted at the time:
{reference}

New answer:
{candidate}

Which answer serves the request better? Answer "new" if the NEW answer does (more of what was asked, fewer errors,
better evidence), "earlier" if the EARLIER answer does (the new one misses parts, is wrong, vaguer, or not an answer),
"equal" if they are about equally good even if worded differently. Give one sentence of reason naming the main
difference. Judge content, not length or wording. Ignore where files were saved and any file paths: the new answer
ran in a sandbox, so its outputs sit under a different folder by design."""


def candidates(agent_root, task_type=None):
    """The collected real cases with a reference answer; research ones by default."""
    path = regression.cases_path(agent_root)
    try:
        cases = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = [c for c in cases if c.get("request") and c.get("reference")]
    if task_type:
        out = [c for c in out if regression.case_type(c) == task_type]
    return out


def start(store, chat, case):
    """Queue one isolated rerun of a real case: eval context, no benchmark binding, normal tools."""
    request = f"[Eval signal {case['id']} {uuid.uuid4().hex[:8]}] " + case["request"]
    note = ("\n\n[Rerun of a finished task, to see whether the current skills and model still answer it as well. Nothing "
            "from this run goes to Alex or anyone else: sending, delivering, delegating and starting tasks are switched "
            "off. Save files only under your task folder's trial_output. Do the task exactly as asked, as well as you can.]")
    row = store.start(chat, request, case.get("context", "") + note,
                      extra={"eval": {"case": case["id"], "signal": True}, "notify_telegram": False})
    return row["id"]


def compare(judge, case, candidate):
    if not (candidate or "").strip():
        return {"closeness": "further", "reason": "no answer was produced"}
    prompt = SIGNAL_PROMPT.format(request=case["request"][:4000], reference=case["reference"][:10000], candidate=candidate[:10000])
    try:
        out = judge(prompt, SIGNAL_SCHEMA)
        g = json.loads(out[0] if isinstance(out, (tuple, list)) else out)      # the judge may return (text, usage)
    except Exception as exc:                        # noqa: BLE001 - a judge failure is recorded, never fatal
        return {"closeness": "same", "reason": f"judge failed: {type(exc).__name__}: {str(exc)[:120]}", "judge_failed": True}
    if g.get("better") not in BETTER:
        return {"closeness": "same", "reason": "judge gave no verdict", "judge_failed": True}
    return {"closeness": BETTER[g["better"]], "reason": str(g.get("reason", ""))[:300]}


ANSWER_STATES = ("finished", "needs_attention")      # a prose rerun has no declared checks, so completion calls it unverified


def answer_text(store, ident):
    """What the rerun wrote, without the runtime's completion verdict or route line: a prose task can never be
    'verified' under the completion rule, so its result arrives as needs_attention with the text after a
    'Completion not verified: ...' paragraph (first signal run, 2026-10-24: all three counted as further unread)."""
    text = regression._read(store.directory(ident) / "result.txt").strip()
    if text.startswith(("Completion not verified:", "Not complete:")):
        text = text.split("\n\n", 1)[1].strip() if "\n\n" in text else ""
    return re.sub(r"\n\[Route\][^\n]*$", "", text).strip()


def run(agent_root, store, chat, judge, sample=3, task_type="research", starter=start, waiter=regression.wait, log=print):
    """Rerun `sample` real cases once each, compare, save, and close the reruns (they are read, not delivered).
    The record is a signal: counts of closer / same / further."""
    cases = candidates(agent_root, task_type) or candidates(agent_root)
    chosen = cases[:sample] if sample else cases
    record = {"started": time.time(), "agent": Path(agent_root).name, "task_type": task_type, "cases": []}
    for case in chosen:
        ident = starter(store, chat, case)
        log(f"{case['id']}: task {ident} queued")
        status = waiter(store, ident)
        answer = answer_text(store, ident) if status in ANSWER_STATES else ""
        c = compare(judge, case, answer) if answer else {"closeness": "further", "reason": f"run ended {status} with no answer"}
        c.update(id=case["id"], task=ident, status=status, task_type=regression.case_type(case))
        if status != "finished":
            try:
                store.control(chat, ident, "cancel", "Signal rerun read and closed; nothing needs attention.")
            except Exception as exc:                # noqa: BLE001 - closing is housekeeping
                log(f"{case['id']}: could not close task {ident}: {type(exc).__name__}")
        record["cases"].append(c)
        log(f"{case['id']}: {status}, {c['closeness']}: {c['reason'][:90]}")
    record["finished"] = time.time()
    record["counts"] = {k: sum(c["closeness"] == k for c in record["cases"]) for k in CLOSENESS}
    record["n"] = len(record["cases"])
    out = Path(agent_root) / "task_state" / "research_signal"
    out.mkdir(parents=True, exist_ok=True)
    path = out / (time.strftime("%Y%m%d-%H%M%S") + ".json")
    n = 0
    while path.exists():
        n += 1; path = out / (time.strftime("%Y%m%d-%H%M%S") + f"_{n}.json")
    path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    record["path"] = str(path)
    return record


def history(agent_root, limit=3):
    out = Path(agent_root) / "task_state" / "research_signal"
    runs = []
    for p in (sorted(out.glob("*.json")) if out.exists() else [])[-limit:]:
        try:
            runs.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return runs


def latest(agent_root):
    """For the dashboard: the newest run's counts and whether the last three runs lean 'further'."""
    runs = history(agent_root, 3)
    if not runs:
        return None
    r = runs[-1]
    further_runs = sum(1 for x in runs if x.get("counts", {}).get("further", 0) > x.get("counts", {}).get("closer", 0))
    return {"time": r.get("finished") or r.get("started"), "n": r.get("n"), "counts": r.get("counts", {}),
            "runs": len(runs), "further_runs": further_runs, "task_type": r.get("task_type"),
            "drifting": len(runs) >= 3 and further_runs == len(runs)}


def table(record):
    c = record["counts"]
    lines = [f"{record['agent']} research signal ({record['task_type']}): {c['closer']} closer, {c['same']} same, "
             f"{c['further']} further of {record['n']}. A signal, not a score."]
    for x in record["cases"]:
        lines.append(f"  {x['id']:12} {x['closeness']:8} {x['reason'][:100]}")
    return "\n".join(lines)
