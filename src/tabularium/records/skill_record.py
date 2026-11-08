"""An agent's track record with a skill, shown when it loads the skill (2026-10-21).

Lessons record what went wrong; nothing recorded what went right. On 2026-10-03 Cato designed a brand logo with
the logo-designer skill (SVG through build_logo_*.py scripts, six rounds of Alex's feedback). On 2026-10-21 he
loaded the same skill for another logo, used an image model instead, and was blocked twice. Alex: [quote removed] Now every skill load comes with the agent's three most recent uses of that skill:
when, what the work was, what it produced, how it ended, and Alex's next message. Built from the agent's own tool
receipts; no model call. About 250 tokens per load.

Every load is noted in <agent>/task_state/skill_uses.sqlite3 as it happens; backfill() indexes the past once from the
foreground receipts, live background task folders and the zipped archive (retention.py zips tasks after 14 days).
"""
import json
import re
import sqlite3
import time
import zipfile
from contextlib import closing
from datetime import datetime
from pathlib import Path

LIMIT = 3
PRODUCED_TOOLS = {"write_file", "edit_file"}
SCRIPT = re.compile(r"[\w\-]+\.(?:py|js|mjs|ps1)\b")


def _db(agent_dir):
    path = Path(agent_dir) / "task_state" / "skill_uses.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=10)
    con.execute("CREATE TABLE IF NOT EXISTS uses(skill TEXT, scope TEXT, ref INTEGER, chat INTEGER, at REAL, "
                "UNIQUE(skill, scope, ref))")
    return con


def note(agent_dir, skill, scope, ref, chat, at=None):
    """One row per skill per task ('task', task id) or foreground turn ('turn', inbox job id); the first load wins."""
    if not ref:
        return
    with closing(_db(agent_dir)) as con, con:
        con.execute("INSERT OR IGNORE INTO uses VALUES (?,?,?,?,?)", (skill, scope, int(ref), chat, at or time.time()))


# ── receipts ────────────────────────────────────────────────────────────────────────────────────────────────

def _receipt_files(agent_dir):
    """Yield (scope, ref_hint, at, loader) for every receipt on disk: foreground, live tasks, archived tasks."""
    root = Path(agent_dir) / "task_state"
    for f in (root / "actions").glob("*.json"):
        yield "turn", None, f, (lambda f=f: f.read_text(encoding="utf-8"))
    background = root / "background"
    for d in background.iterdir() if background.exists() else []:
        if d.is_dir() and d.name.isdigit():
            for f in (d / "task_state" / "actions").glob("*.json"):
                yield "task", int(d.name), f, (lambda f=f: f.read_text(encoding="utf-8"))
    for z in (background / "archive").glob("*.zip") if (background / "archive").exists() else []:
        if not z.stem.isdigit():
            continue
        try:
            with zipfile.ZipFile(z) as zf:
                for name in zf.namelist():
                    n = name.replace("\\", "/")
                    if "/actions/" in n and n.endswith(".json"):
                        data = zf.read(name).decode("utf-8", "replace")
                        yield "task", int(z.stem), Path(n), (lambda data=data: data)
        except (OSError, zipfile.BadZipFile):
            continue


def _stamp(path):
    try:
        return int(Path(path).stem) / 1e9
    except ValueError:
        return 0.0


def backfill(agent_dir):
    """Index past skill loads from every receipt. Safe to rerun. Returns the number of rows added."""
    added = 0
    with closing(_db(agent_dir)) as con, con:
        for scope, ref, path, load in _receipt_files(agent_dir):
            try:
                text = load()
                if '"skill"' not in text:
                    continue
                r = json.loads(text)
            except (OSError, ValueError):
                continue
            if r.get("tool") != "skill":
                continue
            skill = (r.get("inputs") or {}).get("name")
            ref = ref if scope == "task" else r.get("job")
            if skill and ref:
                added += con.execute("INSERT OR IGNORE INTO uses VALUES (?,?,?,?,?)",
                                     (skill, scope, int(ref), r.get("chat"), _stamp(path))).rowcount
    return added


def _work_receipts(agent_dir, scope, ref, at):
    """The receipts of one task, or of one foreground turn (receipts within six hours after the load, same job)."""
    out = []
    root = Path(agent_dir) / "task_state"
    if scope == "task":
        folder = root / "background" / str(ref) / "task_state" / "actions"
        if folder.exists():
            out = [json.loads(f.read_text(encoding="utf-8")) for f in sorted(folder.glob("*.json"))]
        else:
            z = root / "background" / "archive" / f"{ref}.zip"
            if z.exists():
                with zipfile.ZipFile(z) as zf:
                    names = sorted(n for n in zf.namelist() if "/actions/" in n.replace("\\", "/") and n.endswith(".json"))
                    out = [json.loads(zf.read(n)) for n in names]
    else:
        lo, hi = int(at * 1e9), int((at + 6 * 3600) * 1e9)
        for f in sorted((root / "actions").glob("*.json")):
            try:
                if lo <= int(f.stem) <= hi:
                    r = json.loads(f.read_text(encoding="utf-8"))
                    if r.get("job") == ref:
                        r["_at"] = int(f.stem) / 1e9
                        out.append(r)
            except (ValueError, OSError):
                continue
    return out


def _produced(receipts):
    names = []
    for r in receipts:
        inputs = r.get("inputs") or {}
        if isinstance(inputs, str):
            continue
        if r.get("tool") in PRODUCED_TOOLS and inputs.get("path"):
            names.append(Path(str(inputs["path"])).name)
        elif r.get("tool") == "execute_command":
            names += SCRIPT.findall(str(inputs.get("command", "")))
    return list(dict.fromkeys(names))[:5]


def _short(text, n):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def _next_owner_message(agent_dir, chat, after):
    """Alex's first message after `after` (epoch), within three days: his reaction, or the next thing he raised."""
    path = Path(agent_dir) / "task_state" / "conversation_recall.sqlite3"
    if not path.exists() or not chat:
        return ""
    since = datetime.fromtimestamp(after).isoformat(timespec="minutes")
    until = datetime.fromtimestamp(after + 3 * 86400).isoformat(timespec="minutes")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute("SELECT text FROM messages WHERE chat=? AND speaker='user' AND said_at>? AND said_at<? "
                         "ORDER BY said_at, id LIMIT 1", (str(chat), since, until)).fetchone()
    text = re.sub(r"^\[[^\]]*\]\s*", "", row[0]) if row else ""          # drop the timestamp prefix
    text = re.sub(r"^\[Alex is replying[^\]]*\]\s*", "", text)
    return _short(text, 90)


def record(agent_dir, skill, chat, scope, ref, store=None, inbox=None, limit=LIMIT):
    """The text appended to a skill load: up to `limit` earlier uses other than the current work. Background tasks
    come before chat turns (a task holds the work; the turn that started it holds little): the most recent ones,
    plus the first time the skill was used, so the original method stays visible however much recent use there is
    (2026-10-21: three Badge attempts that day would otherwise have hidden a brand logo of 2026-10-03)."""
    with closing(_db(agent_dir)) as con:
        rows = con.execute("SELECT scope, ref, chat, at FROM uses WHERE skill=? AND NOT (scope=? AND ref=?) "
                           "ORDER BY at DESC", (skill, scope, int(ref or 0))).fetchall()
    if not rows:
        return ""
    def real_work(r):
        """Diagnoses, trials and eval reruns load skills to examine them; they are not precedent."""
        if store is None:
            return True
        try:
            from tabularium.records.background_runtime import meta
            return not meta(store.get(r[1])["request"])
        except (ValueError, KeyError):
            return True
    tasks = [r for r in rows if r[0] == "task" and real_work(r)]
    turns = [r for r in rows if r[0] == "turn"]
    picked = tasks[:max(1, limit - 1)]
    first = tasks[-1] if tasks else None
    if first and first not in picked:
        picked.append(first)
    picked += turns[:max(0, limit - len(picked))]
    picked = sorted(picked[:limit], key=lambda r: -r[3])
    lines = []
    for use_scope, use_ref, use_chat, at in picked:
        what, status, ended = "", "", at
        if use_scope == "task" and store is not None:
            try:
                task = store.get(use_ref)
                what, status, ended = task["request"], task["status"], task["updated"] or at
            except (ValueError, KeyError):
                pass
        elif use_scope == "turn" and inbox is not None:
            try:
                with inbox.db() as db:
                    row = db.execute("SELECT payload FROM jobs WHERE id=?", (use_ref,)).fetchone()
                what = (json.loads(row[0]).get("text") or "") if row else ""
            except (sqlite3.Error, ValueError):
                pass
        receipts = _work_receipts(agent_dir, use_scope, use_ref, at)
        if use_scope == "turn" and receipts:
            ended = max(r.get("_at", at) for r in receipts)
        made = _produced(receipts)
        reaction = _next_owner_message(agent_dir, use_chat or chat, ended)
        label = f"task {use_ref}" if use_scope == "task" else "chat turn"
        if first is not None and (use_scope, use_ref) == (first[0], first[1]) and len(tasks) > 1:
            label += ", the first time you used this skill"
        line = f"- {datetime.fromtimestamp(at):%Y-%m-%d}, {label}: \"{_short(what, 100)}\""
        if made:
            line += " — produced " + ", ".join(made)
        if status:
            line += f" — {status}"
        if reaction:
            line += f". Alex's next message: \"{reaction}\""
        lines.append(line)
    return ("\n\n## Your earlier work with this skill\n"
            "What you did before and how it went. Reuse a method that worked before inventing a new one.\n"
            + "\n".join(lines))
