"""Weekly scorecard from records the system already writes (2026-10-16). No model calls.

Per agent, per local week (Monday to Sunday): turns answered and turns that failed; replies the runtime bounced
(unbacked promise, cut-off, step limit, runaway stop; recorded as usage_events of kind "bounce" at the moment they
happen, so no history is parsed and Tacitus's conversations are never read); what a foreground turn costs and how often
the agent put a question or a parked action to the owner; background tasks finished and stopped,
with the median time to finish; Cato's acceptance of Cal's and Vit's work (items met, reworks, wrong guesses, real
cost against the agreed target); the
memory exam; and corrections: lessons (candidates are the workers' own failure notes, taken ones went through Alex)
and incident records that name the agent. The last line only
counts what was written down: a week with no lessons and no incidents is clean only if we kept the records.
Four of these are what a change to the framework is judged by, week on week (2026-11-25): the share of turns the owner
corrected, effective tokens per turn, delegation cost against target, and how often he was asked.
Usage: python -m tabularium.ops.scorecard [weeks]  (the dashboard shows the same table on the overview)."""
from datetime import datetime, timedelta
import json
import re
import sqlite3
import statistics
import sys
from tabularium.paths import home as _home
from contextlib import closing
from pathlib import Path
from zoneinfo import ZoneInfo

from tabularium.core.roster import AGENTS, ROLES
from tabularium.models.usage import decode
from tabularium.records.task_cost import effective_tokens
from tabularium.learning import review_exam

LOCAL = ZoneInfo("UTC")
STOPPED = ("needs_attention", "interrupted", "capped", "infeasible", "cancelled")
BOUNCES = ("unbacked_promise", "cut_off", "step_limit", "runaway")
# Incident records name people, not slots: a slot matches its name, its persona's display name and the short
# forms people use for that persona (keyed by display name so the export's slot rename leaves them alone).
SHORT = {"Callimachus": ("cal",), "Vitruvius": ("vit",)}


def aliases(name):
    import importlib
    display = importlib.import_module("tabularium.agents." + ROLES[name].entry).CONFIG.display
    return (name, display.lower(), *SHORT.get(display, ()))


def week_bounds(now=None, weeks_back=0):
    """(start, end) unix times of the local week containing `now`, shifted back `weeks_back` weeks."""
    now = datetime.fromtimestamp(now, LOCAL) if isinstance(now, (int, float)) else (now or datetime.now(LOCAL))
    monday = (now - timedelta(days=now.weekday(), weeks=weeks_back)).replace(hour=0, minute=0, second=0, microsecond=0)
    return monday.timestamp(), (monday + timedelta(days=7)).timestamp()


def _query(path, sql, args=()):
    if not Path(path).exists():
        return []
    with closing(sqlite3.connect(path, timeout=15)) as db:
        db.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in db.execute(sql, args)]
        except sqlite3.OperationalError:          # a table this store never created
            return []


def agent_week(root, name, start, end):
    root = Path(root); state = root / name / "task_state"
    jobs = _query(state / "inbox.sqlite3", "SELECT status FROM jobs WHERE handler='handle_message' AND created>=? AND created<?", (start, end))
    bounces = _query(state / "inbox.sqlite3", "SELECT model, count(*) n FROM usage_events WHERE kind='bounce' AND created>=? AND created<? GROUP BY model", (start, end))
    responses = _query(state / "inbox.sqlite3", "SELECT count(*) n FROM usage_events WHERE kind='response' AND created>=? AND created<?", (start, end))
    corrections = _query(state / "inbox.sqlite3", "SELECT count(*) n FROM usage_events WHERE kind='correction' AND created>=? AND created<?", (start, end))
    tasks = _query(state / "background" / "jobs.sqlite3", "SELECT status, created, updated FROM tasks WHERE created>=? AND created<?", (start, end))
    finished = [t for t in tasks if t["status"] == "finished"]
    accepted = _query(state / "background" / "jobs.sqlite3",
                      "SELECT agent, items_total, items_met, reworks, guesses_verdict FROM scorecard WHERE accepted>=? AND accepted<?", (start, end))
    logs = _query(state / "background" / "jobs.sqlite3", "SELECT rework_log FROM scorecard WHERE accepted>=? AND accepted<?", (start, end))
    priced = _query(state / "background" / "jobs.sqlite3", "SELECT budget, spent FROM scorecard WHERE accepted>=? AND accepted<? "
                    "AND spent IS NOT NULL AND budget>0", (start, end))
    asked = _query(state / "background" / "jobs.sqlite3", "SELECT count(*) n FROM decisions WHERE created>=? AND created<?", (start, end))
    reviews = [json.loads(r["acceptance"] or "{}").get("providers") for r in _query(
        state / "background" / "jobs.sqlite3", "SELECT d.acceptance FROM delegations d JOIN scorecard s ON s.request_id=d.request_id "
        "WHERE s.accepted>=? AND s.accepted<? GROUP BY d.request_id", (start, end))]
    reviews = [p for p in reviews if p and p.get("worker")]
    usage = _query(state / "inbox.sqlite3", "SELECT metrics, role FROM usage_events WHERE kind='response' AND created>=? AND created<?", (start, end))
    done = sum(j["status"] == "done" for j in jobs)
    corrected = corrections[0]["n"] if corrections else 0
    outcomes = [o for a in logs for e in json.loads(a.get("rework_log") or "[]") for o in (e.get("outcome") or {}).values()]
    lessons = _query(state / "projects.sqlite3", "SELECT json_extract(data,'$.status') s, count(*) n FROM records WHERE kind='lesson' "
                     "AND updated>=? AND updated<? GROUP BY s", (start, end))
    exam = {}
    exam_path = state / "memory_exam.json"
    if exam_path.exists():
        try:
            record = json.loads(exam_path.read_text(encoding="utf-8"))
            if start <= record.get("time", 0) < end:
                exam = {k: record.get(k, 0) for k in ("correct", "partial", "wrong")}
        except ValueError:
            pass
    return {
        "turns": done,
        "turns_failed": sum(j["status"] == "failed" for j in jobs),
        "bounces": {b["model"]: b["n"] for b in bounces},
        "model_responses": responses[0]["n"] if responses else 0,
        "corrections": corrected,                                      # times Alex told the agent it got something wrong
        "correction_rate": round(corrected / done, 3) if done else None,
        # every model response recorded in the foreground that week, weighted as task_cost weighs a task's
        "tokens_per_turn": round(sum(effective_tokens(decode(u["metrics"], u["role"] or "")) for u in usage) / done) if done else None,
        "asked": asked[0]["n"] if asked else 0,                        # questions and parked actions put to Alex
        "delegation_cost": {"priced": len(priced), "spent": sum(p["spent"] for p in priced),
                            "target": sum(p["budget"] for p in priced)} if priced else None,
        "tasks_finished": len(finished),
        "tasks_stopped": sum(t["status"] in STOPPED for t in tasks),
        "task_minutes_median": round(statistics.median((t["updated"] - t["created"]) / 60 for t in finished), 1) if finished else None,
        "delegations": {
            "accepted": len(accepted),
            "items_met": sum(a["items_met"] or 0 for a in accepted),
            "items_total": sum(a["items_total"] or 0 for a in accepted),
            "reworks": sum(a["reworks"] or 0 for a in accepted),
            "wrong_guesses": sum(1 for a in accepted if a["guesses_verdict"] and a["guesses_verdict"] != "ok"),
            # a finding the delegate changed nothing for was noise or a dispute; either way it cost a round
            "rework_changed": outcomes.count("changed"),
            "rework_unchanged": outcomes.count("unchanged"),
            # a review by the provider that did the work shares its blind spots; counted where both are on record
            "reviews_known": len(reviews),
            "reviews_same_provider": sum(p["reviewer"] in p["worker"] for p in reviews),
        } if accepted else None,
        "review_exam": review_exam.latest(root / name, start, end),      # planted defects caught, clean work sent back
        "memory_exam": exam or None,
        # candidates are the workers' own failure notes; the rest went through Alex (proposed, approved, verified, applied)
        "lessons_candidate": sum(l["n"] for l in lessons if l["s"] == "candidate"),
        "lessons_taken": sum(l["n"] for l in lessons if l["s"] != "candidate"),
        "incidents": incidents_naming(root, name, start, end),
    }


def incidents_naming(root, name, start, end):
    """Incident records dated in the week whose Affected row names the agent."""
    count = 0
    for path in sorted((Path(root) / "docs" / "incidents").glob("*.md")):
        m = re.match(r"(\d{4}-\d{2}-\d{2})", path.name)
        if not m:
            continue
        day = datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=LOCAL).timestamp()
        if not start <= day < end:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        affected = re.search(r"\|\s*Affected\s*\|([^\n]*)", text)
        row = (affected.group(1) if affected else text[:600]).lower()
        if any(re.search(r"\b" + alias + r"\b", row) for alias in aliases(name)):
            count += 1
    return count


def weekly(root, now=None, weeks=2):
    out = []
    for back in range(weeks):
        start, end = week_bounds(now, back)
        out.append({"start": start, "end": end,
                    "label": datetime.fromtimestamp(start, LOCAL).strftime("w/c %d %b"),
                    "agents": {name: agent_week(root, name, start, end) for name in AGENTS}})
    return {"weeks": out}


def table(report):
    """Plain-text table: one column per week, rows per agent and metric."""
    weeks = report["weeks"]
    lines = [f"{'':32}" + "".join(f"{w['label']:>14}" for w in weeks)]
    for name in AGENTS:
        lines.append(name)
        rows = [("turns (failed)", lambda a: f"{a['turns']} ({a['turns_failed']})"),
                ("bounced replies", lambda a: str(sum(a["bounces"].values()))),
                ("Alex's corrections (rate)", lambda a: str(a.get("corrections", 0)) + ("" if a.get("correction_rate") is None else f" ({a['correction_rate']:.0%})")),
                ("tokens per turn", lambda a: "-" if a.get("tokens_per_turn") is None else f"{a['tokens_per_turn']:,}"),
                ("asked Alex", lambda a: str(a.get("asked", 0))),
                ("tasks finished / stopped", lambda a: f"{a['tasks_finished']} / {a['tasks_stopped']}"),
                ("median task minutes", lambda a: "-" if a["task_minutes_median"] is None else str(a["task_minutes_median"])),
                ("delegations items met", lambda a: "-" if not a["delegations"] else f"{a['delegations']['items_met']}/{a['delegations']['items_total']} rw{a['delegations']['reworks']}"),
                ("rework items changed/unch.", lambda a: "-" if not a["delegations"] else f"{a['delegations'].get('rework_changed', 0)}/{a['delegations'].get('rework_unchanged', 0)}"),
                ("reviewed by worker's provider", lambda a: "-" if not (a["delegations"] or {}).get("reviews_known") else f"{a['delegations']['reviews_same_provider']}/{a['delegations']['reviews_known']}"),
                ("review exam caught / false", lambda a: "-" if not a.get("review_exam") else f"{a['review_exam']['caught']}/{a['review_exam']['planted']} / {a['review_exam']['false_alarms']}/{a['review_exam']['clean']}"),
                ("delegation cost x target", lambda a: "-" if not a.get("delegation_cost") else f"{a['delegation_cost']['spent'] / a['delegation_cost']['target']:.1f}x ({a['delegation_cost']['priced']})"),
                ("memory exam c/p/w", lambda a: "-" if not a["memory_exam"] else "/".join(str(a["memory_exam"][k]) for k in ("correct", "partial", "wrong"))),
                ("lessons cand/taken + inc", lambda a: f"{a['lessons_candidate']}/{a['lessons_taken']} + {a['incidents']}")]
        for label, f in rows:
            lines.append(f"  {label:30}" + "".join(f"{f(w['agents'][name]):>14}" for w in weeks))
    return "\n".join(lines)


if __name__ == "__main__":
    print(table(weekly(_home(), weeks=int(sys.argv[1]) if len(sys.argv) > 1 else 2)))
