"""Trace of one background task: what it did, in order, from records the runtime already keeps (2026-10-20).

Every tool call a worker makes is receipted in its task_state/actions folder (tool, inputs, status, result), and every
model call is a usage_events row in its own inbox database. Put in time order they are the task's trace: the thing
LangSmith and Phoenix sell, built here from files that exist. Nothing is recorded for the trace; it only reads."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from agent_core.usage import decode, input_total


def events(store, ident, limit=400):
    """[{t, kind, name, detail, status, size, archive}] oldest first for task `ident`."""
    folder = store.directory(ident)
    out = []
    actions = folder / "task_state" / "actions"
    for path in sorted(actions.glob("*.json")) if actions.exists() else []:
        try:
            r = json.loads(path.read_text(encoding="utf-8"))
            t = int(path.stem) / 1e9
        except (ValueError, OSError):
            continue
        result = str(r.get("result", ""))
        archive = ""
        if result.startswith("[Archived tool output: "):
            archive = result[len("[Archived tool output: "):].split("]", 1)[0]
        out.append({"t": t, "kind": "tool", "name": r.get("tool", "?"), "detail": _inputs(r.get("inputs")),
                    "status": r.get("status", "?"), "size": len(result), "archive": archive,
                    "error": str(r.get("error", ""))[:200]})
    db = folder / "task_state" / "inbox.sqlite3"
    if db.exists():
        try:
            with closing(sqlite3.connect(db, timeout=5)) as con:
                rows = con.execute("SELECT created, kind, model, billing, role, metrics FROM usage_events "
                                   "WHERE kind IN ('response','bounce') ORDER BY id").fetchall()
        except sqlite3.Error:
            rows = []
        for created, kind, model, billing, role, metrics in rows:
            try:
                m = decode(metrics, role)
            except ValueError:
                m = {}
            tokens = reported_tokens(m)
            out.append({"t": created, "kind": "model" if kind == "response" else "bounce", "name": model or "",
                        "detail": (role or "") + (f" · {billing}" if billing else ""), "status": kind, "size": tokens,
                        "cache_read": m.get("cache_read"), "archive": "", "error": ""})
    checkpoint = folder / "checkpoint.json"
    if checkpoint.exists():
        try:
            cp = json.loads(checkpoint.read_text(encoding="utf-8"))
            out.append({"t": float(cp.get("updated") or checkpoint.stat().st_mtime), "kind": "checkpoint", "name": "checkpoint",
                        "detail": str(cp.get("completed", ""))[:300], "status": "blocker: " + cp["blocker"] if cp.get("blocker") else "ok",
                        "size": 0, "archive": "", "error": ""})
        except (ValueError, OSError):
            pass
    out.sort(key=lambda e: e["t"])
    return out[-limit:]


def reported_tokens(metrics):
    total = input_total(metrics)
    output = metrics.get('output')
    return total + output if total is not None and output is not None else None


def _inputs(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            try:
                import ast
                value = ast.literal_eval(value)
            except (ValueError, SyntaxError):
                return value[:200]
    if isinstance(value, dict):
        return " · ".join(f"{k}={str(v)[:60]}" for k, v in list(value.items())[:4])
    return str(value)[:200]


def summary(evs):
    tools = [e for e in evs if e["kind"] == "tool"]
    models = [e for e in evs if e["kind"] == "model"]
    span = (evs[-1]["t"] - evs[0]["t"]) if len(evs) > 1 else 0
    return {"tool_calls": len(tools), "model_calls": len(models), "bounces": sum(e["kind"] == "bounce" for e in evs),
            "failed_tools": sum(e["status"] != "finished" for e in tools), "minutes": round(span / 60, 1),
            "tokens": sum(e["size"] for e in models) if all(e['size'] is not None for e in models) else None,
            "unknown_usage": sum(e['size'] is None for e in models),
            "cache_read": sum(e['cache_read'] for e in models) if all(e.get('cache_read') is not None for e in models) else None}
