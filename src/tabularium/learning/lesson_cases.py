"""Every lesson carries its own benchmark case (2026-10-26, Alex: [quote removed]).

A correction becomes a lesson; the lesson now also drafts a case: the task's request as the input, the owner's rule,
and a placeholder for the oracle and the checkable points. The agent fills those in through the lesson tool, the
runtime calibrates the case (RUNS isolated runs per arm: the current skill as it stands, and the same skill with the
rule stated outright), and a case that is broken (mostly fails even when told the rule) or non-discriminating (the
current skill mostly passes already) is refused by code; a band over repeated runs, not one pass/fail pair, since
2026-11-02 (GDPevo's calibration; one flaky run had been refusing sound cases). The rest wait for the owner's quoted yes, after which they join the benchmark
that regression and the lesson trial read. Two or three approved cases of one skill recombine into a reserved case,
drafted by code, so the trial can tell a learned rule from a memorised task (GDPevo's recombination, Cato's review).

Nothing here is a new kind of record: the case is a field on the lesson. Approved cases live in
<agent>/evals/lesson_cases.json, which regression.load_cases reads beside the shared benchmark.
"""
import json
import re
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

from tabularium.learning import learning

from tabularium.learning import regression
CASES_FILE = "lesson_cases.json"
RUNS = 3            # runs per arm in calibration
FLOOR_MAX = 1 / 3   # without the rule: at most this share may pass
CEILING_MIN = 2 / 3 # with the rule stated: at least this share must pass
DRAFT_REVIEWER = "agent draft; awaiting the owner's yes"
SLOTS = ("case", "reserved")


def _task_request(root, source):
    """The original request of the task a lesson came from, when its source is that task's checkpoint file."""
    m = re.search(r"[\\/]background[\\/](\d+)[\\/]", str(source or ""))
    if not m:
        return ""
    try:
        from tabularium.records.background_runtime import TaskStore
        return (TaskStore(Path(root)).get(int(m.group(1))) or {}).get("request", "") or ""
    except Exception:
        return ""


def _message_request(root, chat, source):
    """The request of a foreground lesson: Alex's message the lesson names as its source (message:<id>)."""
    m = re.match(r"message:(\d+)$", str(source or "").strip())
    if not m:
        return ""
    recall = Path(root) / "task_state" / "conversation_recall.sqlite3"
    if not recall.exists():
        return ""
    try:
        with closing(sqlite3.connect(recall.as_uri() + "?mode=ro", uri=True)) as db:
            row = db.execute("SELECT text FROM messages WHERE id=? AND chat=? AND speaker='user'", (int(m.group(1)), str(chat))).fetchone()
    except sqlite3.Error:
        return ""
    return re.sub(r"^\[[^\]]*\]\s*", "", row[0]).strip() if row else ""


def draft(root, chat, key):
    """Called by learning.propose: the case drafted from the lesson, filled in later by the agent. The request is the
    task's, or Alex's message when the lesson names it (2026-11-02: 208 of 230 lessons had no case at all, and the
    drafted ones had an empty request whenever the lesson came from a conversation, not a task)."""
    def change(d):
        if d.get("case"):
            return
        request = _task_request(root, d.get("source")) or _message_request(root, chat, d.get("source"))
        d["case"] = {"status": "draft", "request": request, "rule": d.get("failure") or d.get("title") or "",
                     "oracle_basis": "", "requirements": [], "split": "development", "from": [key], "version": 1}
    learning._update(root, chat, key, change)


def case_id(key, slot):
    return f"{key}-{'reserved' if slot == 'reserved' else 'case'}"


def record(key, d, slot="case"):
    """The benchmark case assembled from a lesson's slot, in the shape regression.validate_case checks."""
    c = d.get(slot) or {}
    reviewed = c.get("reviewed_by") or DRAFT_REVIEWER
    return dict(id=case_id(key, slot), version=c.get("version", 1), task_type=(d.get("task_types") or ["other"])[0],
                request=c.get("request", ""), rule=c.get("rule", ""), split=c.get("split", "development"),
                lesson_from=c.get("from", [key]), oracle=dict(basis=c.get("oracle_basis", ""), reviewed_by=reviewed, version=c.get("version", 1)),
                requirements=c.get("requirements", []), **({"fixtures": c["fixtures"]} if c.get("fixtures") else {}))


def fill(root, chat, key, slot="case", request=None, oracle_basis=None, requirements=None, fixtures=None):
    """The agent's draft of the oracle, the checkable points and the fixtures the run needs (regression.validate_fixtures).
    Validated as a benchmark case; status back to draft."""
    if slot not in SLOTS:
        raise ValueError("slot is case or reserved.")

    def change(d):
        c = d.get(slot)
        if not c:
            raise ValueError("No such case on this lesson." if slot == "reserved" else "The lesson has no drafted case.")
        if c.get("status") == "approved":
            raise ValueError("An approved case is frozen; a changed case is a new version through a new lesson.")
        if request is not None:
            c["request"] = " ".join(str(request).split())
        if oracle_basis is not None:
            c["oracle_basis"] = str(oracle_basis).strip()
        if requirements is not None:
            c["requirements"] = requirements
        if fixtures is not None:
            c["fixtures"] = fixtures
        if not c.get("request"):
            raise ValueError("The case needs a request: the task as the owner would ask it.")
        regression.validate_case(record(key, dict(d, **{slot: c}), slot))
        c["status"] = "draft"
        c.pop("calibration", None)
        return record(key, d, slot)
    case = learning._update(root, chat, key, change)
    return dict(key=key, slot=slot, status="draft", case_id=case["id"], requirements=len(case["requirements"]),
                next=f"calibrate: {RUNS} isolated runs per arm decide whether the case can tell the rule apart; then the owner's yes adds it to the benchmark.")


def calibrate(root, chat, key, store, system_prompt, slot="case", max_steps=12, runs=RUNS):
    """RUNS isolated runs per arm of the drafted case with the lesson's skill as it stands: floor (the request alone)
    and ceiling (the request with the rule stated outright). Graded by reconcile() against the band once all settle."""
    root = Path(root).resolve()
    if slot not in SLOTS:
        raise ValueError("slot is case or reserved.")
    d = learning.read(root, chat, key)
    c = d.get(slot) or {}
    if c.get("status") not in ("draft", "calibrated", "rejected"):
        raise ValueError("Only a drafted case can be calibrated." if c else "The lesson has no such case.")
    if (c.get("calibration") or {}).get("runs") and not c["calibration"].get("done"):
        raise ValueError("A calibration is already running.")
    case = record(key, d, slot)
    regression.validate_case(case)
    p = d.get("proposal") or {}
    if not p.get("method_path"):
        raise ValueError("Propose the skill edit first: calibration runs the case against that skill as it stands.")
    method = Path(p["method_path"]).resolve()
    learning.still_applies(method, p)
    baseline = method.read_text(encoding="utf-8")
    folder = root / "task_state" / "calibrations" / key / uuid.uuid4().hex
    folder.mkdir(parents=True)
    skill = folder / "baseline.md"
    skill.write_text(baseline, encoding="utf-8")
    from tabularium.learning import trials
    prompt = trials.skill_prompt(system_prompt, baseline, baseline)
    calibration = dict(folder=str(folder), case_id=case["id"], runs=[], done=False, started=time.time(), max_steps=max_steps)
    for arm in ("floor", "ceiling"):
        request = case["request"] + ("" if arm == "floor" else "\n\nRule to apply, stated by the owner: " + case["rule"])
        trial = dict(lesson=key, cohort=str(folder), arm=arm, max_steps=max_steps, skill=str(skill), skill_sha=learning._sha(skill),
                     system_prompt=prompt, overrides={str(method): str(skill)}, calibration=slot, fixtures=case.get("fixtures") or {})
        for n in range(1, max(1, int(runs)) + 1):
            row = store.start(chat, f"[Calibration {folder.name}/{arm}-{n}] " + request, "", extra={"trial": trial, "notify_telegram": False})
            calibration["runs"].append(dict(task=row["id"], arm=arm))

    def change(d):
        d[slot]["status"] = "calibrating"
        d[slot]["calibration"] = calibration
    learning._update(root, chat, key, change)
    return dict(key=key, slot=slot, status="calibrating", tasks=[r["task"] for r in calibration["runs"]],
                note="Floor and ceiling runs queued in isolation; the verdict is recorded when all settle and shows in the digest.")


def verdict(floor, ceiling):
    """The calibration band over repeated runs. Broken: with the rule stated outright fewer than CEILING_MIN of the
    runs pass. Not discriminating: without it more than FLOOR_MAX already pass. One run per arm (old records) grades
    as before: a single pass or fail."""
    up, down = sum(bool(g["passed"]) for g in ceiling), sum(bool(g["passed"]) for g in floor)
    if up < CEILING_MIN * len(ceiling):
        return "rejected", f"broken: with the rule stated outright only {up} of {len(ceiling)} runs passed"
    if down > FLOOR_MAX * len(floor):
        return "rejected", f"not discriminating: the current skill already passes {down} of {len(floor)} runs"
    return "calibrated", f"the rule makes the difference: {down} of {len(floor)} passed without it, {up} of {len(ceiling)} with it"


def reconcile(store):
    """Grade settled calibrations (called from the task monitor beside trials.reconcile). Nothing is sent."""
    db = learning.ProjectStore(store.agent_dir).connect()
    try:
        rows = list(db.execute("SELECT chat,key,data FROM records WHERE kind='lesson' AND "
                               "(json_extract(data, '$.case.status')='calibrating' OR json_extract(data, '$.reserved.status')='calibrating')"))
    finally:
        db.close()
    for row in rows:
        d = json.loads(row["data"])
        for slot in SLOTS:
            c = d.get(slot) or {}
            cal = c.get("calibration") or {}
            if c.get("status") != "calibrating" or cal.get("done") or not cal.get("runs"):
                continue
            tasks = [store.get(r["task"]) for r in cal["runs"]]
            if any(t["status"] in regression.ACTIVE or store.lease_busy(t["id"]) for t in tasks):
                continue
            case = record(row["key"], d, slot)
            grades = {"floor": [], "ceiling": []}
            for run, task in zip(cal["runs"], tasks):
                folder = store.directory(task["id"])
                answer = folder / "result.txt"
                text = answer.read_text(encoding="utf-8") if answer.exists() else ""
                g = regression.grade(None, case, text, folder / "trial_output")
                if task["status"] != "finished":
                    g.update(passed=False, quality=0, note="Run did not finish: " + task["status"])
                g.update(task=task["id"], status=task["status"])
                grades[run["arm"]].append(g)
            status, reason = verdict(grades["floor"], grades["ceiling"])
            cal.update(done=True, finished=time.time(), grades=grades, verdict=reason)
            (Path(cal["folder"]) / "evidence.json").write_text(json.dumps(dict(case=case, calibration=cal), ensure_ascii=False, indent=2), encoding="utf-8")

            def change(dd, slot=slot, status=status, cal=cal):
                dd[slot]["status"] = status
                dd[slot]["calibration"] = cal
            learning._update(store.agent_dir, row["chat"], row["key"], change)


def cases_path(root):
    return Path(root) / "evals" / CASES_FILE


def approved_cases(root):
    path = cases_path(root)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return []


def approve(root, chat, key, source_id, quote, slot="case"):
    """The owner's yes, in his own words, adds a calibrated case to the benchmark. Frozen from then on."""
    root = Path(root).resolve()
    learning.owner_said(root, chat, source_id, quote)
    if slot not in SLOTS:
        raise ValueError("slot is case or reserved.")

    def change(d):
        c = d.get(slot) or {}
        if c.get("status") != "calibrated":
            raise ValueError("Only a calibrated case can be approved; calibrate it first." if c else "No such case.")
        c["status"] = "approved"
        c["reviewed_by"] = f"owner message {int(source_id)}"
        c["approval"] = {"source": f"message:{int(source_id)}", "quote": quote, "at": time.time()}
        return record(key, d, slot)
    case = learning._update(root, chat, key, change)
    regression.validate_case(case)
    cases = [c for c in approved_cases(root) if c["id"] != case["id"]] + [case]
    path = cases_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cases, ensure_ascii=False, indent=1), encoding="utf-8")
    return dict(key=key, slot=slot, status="approved", case_id=case["id"], benchmark_cases=len(cases))


def skill_of(d):
    return str(Path((d.get("proposal") or d.get("applied") or {}).get("method_path", "")).resolve()) if (d.get("proposal") or d.get("applied")) else ""


def recombine(root, chat, key, with_keys):
    """Draft a reserved case on this lesson from two or three approved development cases of the same skill: this
    lesson's request plus the others' rules, and every point that can be checked on this lesson's output. Drafted by
    code; the agent revises it with fill(slot='reserved'); calibration then decides whether it holds."""
    root = Path(root).resolve()
    keys = [key] + [k for k in with_keys if k != key]
    if not 2 <= len(keys) <= 3:
        raise ValueError("Recombine this lesson with one or two other lessons.")
    lessons = [(k, learning.read(root, chat, k)) for k in keys]
    skills = {skill_of(d) for _, d in lessons}
    if len(skills) != 1 or "" in skills:
        raise ValueError("Recombine lessons of one skill only; each needs a proposed or applied edit naming the skill.")
    for k, d in lessons:
        if (d.get("case") or {}).get("status") != "approved":
            raise ValueError(f"{k}: only approved development cases recombine.")
    base = lessons[0][1]["case"]
    others = [d["case"] for _, d in lessons[1:]]
    paths = {r["check"].get("path") for r in base["requirements"] if r.get("check", {}).get("kind") != "judgment"}
    requirements = list(base["requirements"])
    for n, other in enumerate(others, 2):
        for r in other["requirements"]:
            check = r.get("check") or {}
            if check.get("kind") == "judgment" or check.get("path") in paths:
                requirements.append(dict(r, id=f"r{n}-{r['id']}"))
    draft_case = {"status": "draft", "split": "reserved", "from": keys, "version": 1,
                  "request": base["request"] + "\n\nAlso: " + " ".join(o["rule"].rstrip(".") + "." for o in others),
                  "rule": "; ".join(d["case"]["rule"] for _, d in lessons),
                  "oracle_basis": base["oracle_basis"] + " Plus, from the recombined rules: " + " ".join(o["oracle_basis"] for o in others),
                  "requirements": requirements}

    def change(d):
        if (d.get("reserved") or {}).get("status") in ("calibrating", "approved"):
            raise ValueError("This lesson already carries a reserved case that is calibrating or approved.")
        d["reserved"] = draft_case
        return record(key, d, "reserved")
    case = learning._update(root, chat, key, change)
    return dict(key=key, slot="reserved", status="draft", case_id=case["id"], from_lessons=keys,
                request=case["request"], requirements=len(requirements),
                next="Read the drafted request and points; revise with action=case slot=reserved if needed, then calibrate.")


def default_spec(root, chat, key, spec):
    """A trial without chosen cases runs on the lesson's own skill: approved development cases of that skill as
    development, approved reserved cases of that skill as held-out."""
    if isinstance(spec, dict) and (spec.get("development_cases") or spec.get("heldout_cases")):
        return spec
    d = learning.read(root, chat, key)
    skill = skill_of(d)
    if not skill:
        return spec
    development, heldout = [], []
    with closing(learning.ProjectStore(Path(root).resolve()).connect()) as db:
        rows = list(db.execute("SELECT key,data FROM records WHERE chat=? AND kind='lesson'", (chat,)))
    for row in rows:
        other = json.loads(row["data"])
        if skill_of(other) != skill:
            continue
        if (other.get("case") or {}).get("status") == "approved":
            development.append(case_id(row["key"], "case"))
        if (other.get("reserved") or {}).get("status") == "approved":
            heldout.append(case_id(row["key"], "reserved"))
    out = dict(spec or {})
    out.update(development_cases=sorted(development), heldout_cases=sorted(heldout))
    return out


def digest_lines(rows):
    """Lines for the weekly digest: cases awaiting the owner's yes, and drafts the agent still has to fill."""
    lines = []
    for key, d in rows:
        for slot in SLOTS:
            c = d.get(slot) or {}
            if c.get("status") == "calibrated":
                lines.append(f"- CASE? {case_id(key, slot)}: calibrated ({(c.get('calibration') or {}).get('verdict', '')}); say yes to add it to the benchmark")
            elif c.get("status") == "rejected":
                lines.append(f"- case {case_id(key, slot)} refused: {(c.get('calibration') or {}).get('verdict', '')}")
    unfilled = sum(1 for _, d in rows if (d.get("case") or {}).get("status") == "draft" and not (d.get("case") or {}).get("requirements"))
    if unfilled:
        lines.append(f"- {unfilled} drafted case(s) still need an oracle and checkable points from the agent.")
    return lines
