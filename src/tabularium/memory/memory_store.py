"""Atomic, conflict-checked edits to the existing single memory file."""
import functools
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from contextlib import contextmanager

_local = threading.local()
_mutex = threading.RLock()

@contextmanager
def memory_lock(path):
    # Re-entrant in this process; the file lock also covers background workers.
    with _mutex:
        if getattr(_local, "held", False):
            yield
            return
        import msvcrt
        lock = Path(str(path) + ".lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        with lock.open("a+b") as f:
            if f.tell() == 0:
                f.write(b"0"); f.flush()
            deadline = time.monotonic() + 30
            while True:
                f.seek(0)
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise TimeoutError("Memory busy; no change made.")
                    time.sleep(.05)
            _local.held = True
            try:
                yield
            finally:
                _local.held = False
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)

def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text); f.flush(); os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)

def backup(path, text):
    destination = Path(path).parent / "memory_backups" / f"memory_{time.time_ns()}.md"
    atomic_text(destination, text)

def locked(path):
    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            with memory_lock(path):
                return fn(*args, **kwargs)
        return wrapped
    return decorate

SUPERSEDED = re.compile(r"(?ms)^## Superseded facts[^\n]*\n.*?(?=^## |\Z)")   # the archive section, wherever it sits


def superseded_section(text):
    """The archive section's match, or None."""
    return SUPERSEDED.search(text)


def current_facts(text):
    """The memory text without its archive section: what counts as current."""
    return SUPERSEDED.sub("", text)


def reconcile(path, old, new, reason=""):
    """Exact replacement, never guess at semantic equivalence. Archive superseded facts."""
    path = Path(path)
    with memory_lock(path):
        text = path.read_text(encoding="utf-8")
        old, new = old.strip(), new.strip()
        if not old:
            return "Read the relevant memory section and supply the exact old text first."
        if old == new:
            return "Already current; no change."
        archive = superseded_section(text)
        current = text if not archive else text[:archive.start()] + text[archive.end():]
        if current.count(old) != 1:
            return "Memory changed or match is ambiguous. Nothing written; read the section again."
        if not new:
            return "Use delete_memory_section for an explicitly requested deletion."
        backup(path, text)
        if archive:
            before, after = text[:archive.start()], text[archive.end():]
            if old in before:
                updated = before.replace(old, new, 1) + archive.group() + after
            else:
                updated = before + archive.group() + after.replace(old, new, 1)
        else:
            updated = text.replace(old, new, 1)
        # Historical text stays below the on-demand marker, clearly labelled as superseded.
        marker = "<!-- MEMORY-DETAIL-BELOW"
        if marker not in updated:
            updated += "\n\n<!-- MEMORY-DETAIL-BELOW -->\n"
        heading = "## Superseded facts (historical, not current)\n"
        entry = f"\n- Superseded {time.strftime('%Y-%m-%d %H:%M')}: {reason.strip() or 'updated fact'}\n"
        entry += "\n".join("> " + line for line in old.splitlines()) + "\n"
        if heading not in updated:
            updated += "\n" + heading
        # Put the entry inside this section, even if later sections already exist.
        begin = updated.index(heading) + len(heading)
        updated = updated[:begin] + entry + updated[begin:]
        atomic_text(path, updated)
        return "Current fact updated; prior wording retained as explicitly superseded history."

def append_unique(path, content):
    path = Path(path)
    with memory_lock(path):
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        content = content.strip()
        if not content:
            return "No content supplied; nothing changed."
        current = current_facts(text)
        if content in current:
            return "Already in memory; no duplicate added."
        # Reusing a section title requires reconciliation instead of another parallel section.
        titles = re.findall(r"(?m)^#{1,6} .+$", content)
        if any(title in text.splitlines() for title in titles):
            return "That memory heading already exists. Read it and use reconcile_memory to update it."
        backup(path, text)
        atomic_text(path, text.rstrip() + "\n\n" + content + "\n")
        return "New memory appended. For changes to existing facts, use reconcile_memory."


def record_fact(path, recall, chat, content, kind, source, quote, valid_from="", valid_to="", supersedes=""):
    """Store provenance in the existing memory file; validate source identity and verbatim quote."""
    import json
    import hashlib
    from contextlib import closing
    from datetime import datetime, timezone
    kinds = {"user_preference", "user_decision", "assistant_proposal", "observation", "inference"}
    if kind not in kinds or not content.strip() or not quote.strip():
        raise ValueError("Supply a supported kind, content and exact source quote.")
    if len(content) > 2000 or len(quote) > 3000:
        raise ValueError("Keep a fact and its supporting quote compact.")
    for value in (valid_from, valid_to):
        if value:
            datetime.strptime(value, "%Y-%m-%d")
    if valid_from and valid_to and valid_to < valid_from:
        raise ValueError("Validity end precedes start.")
    if source.startswith("message:"):
        ident = int(source.split(":", 1)[1])
        with closing(recall.connect()) as db:
            row = db.execute("SELECT * FROM messages WHERE id=? AND chat=?", (ident, str(chat))).fetchone()
        if row is None or quote not in row["text"]:
            raise ValueError("Source message or exact quote not found for this chat.")
        if kind.startswith("user_") and row["speaker"] != "user":
            raise ValueError("Assistant text cannot establish Alex's decision or preference.")
        if kind == "assistant_proposal" and row["speaker"] != "assistant":
            raise ValueError("An assistant proposal requires an assistant source.")
        if kind == "observation":
            raise ValueError("Tool observations require a saved action record.")
        evidence = {"source": source, "speaker": row["speaker"], "said_at": row["said_at"],
                    "date_kind": row["date_kind"], "quote": quote}
    else:
        source_path = Path(source).resolve()
        root = Path(path).parent.resolve() / "task_state"
        if not source_path.is_relative_to(root) or source_path.parent.name != "actions" or source_path.suffix != ".json":
            raise ValueError("Use message:<id> or a saved task_state/.../actions/*.json record.")
        record = json.loads(source_path.read_text(encoding="utf-8"))
        body = record.get("result", "")
        body = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        if record.get("chat") != chat or record.get("status") != "finished" or quote not in body:
            raise ValueError("Action must be finished for this chat and contain the exact quote.")
        if kind not in {"observation", "inference"}:
            raise ValueError("Tool output cannot establish user decisions or preferences.")
        evidence = {"source": str(source_path), "tool": record.get("tool"), "job": record.get("job"),
                    "source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(), "quote": quote}
    # Quote matching verifies provenance only, not truth or semantic entailment.
    payload = {"content": content.strip(), "kind": kind, "evidence": evidence,
               "valid_from": valid_from or None, "valid_to": valid_to or None,
               "verification": "source quote matched; claim interpretation not independently verified",
               "supersedes": supersedes or None}
    ident = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
    payload["recorded_at"] = datetime.now(timezone.utc).isoformat()
    fence = chr(96) * 3
    block = "### Fact " + ident + "\n" + fence + "json\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n" + fence
    path = Path(path)
    with memory_lock(path):
        text = path.read_text(encoding="utf-8")
        if "### Fact " + ident in text:
            return "Already recorded: " + ident
        if supersedes:
            if not re.fullmatch(r"[0-9a-f]{16}", supersedes):
                raise ValueError("Supply the exact prior fact ID.")
            current = current_facts(text)
            match = re.search(r"(?ms)^### Fact " + supersedes + "\n" + fence + r"json\n.*?\n" + fence, current)
            if not match:
                raise ValueError("Current prior fact not found; read memory before superseding.")
            return reconcile(path, match.group(), block, "Replaced by sourced fact " + ident)
        backup(path, text)
        if "<!-- MEMORY-DETAIL-BELOW" not in text:
            text += "\n\n<!-- MEMORY-DETAIL-BELOW -->\n"
        # Keep records outside the historical section, even when that section ends the file.
        heading = "## Sourced facts\n"
        if heading not in text:
            text = text.rstrip() + "\n\n" + heading
        begin = text.index(heading) + len(heading)
        atomic_text(path, text[:begin] + "\n" + block + "\n" + text[begin:])
    return "Recorded fact " + ident + " in memory.md. Source checked; truth not automatically verified."


def delete_section(path, heading):
    """Remove one markdown section (heading to the next heading of the same or higher level). Maintenance
    operation since 2026-10-08: run by Alex or Claude Code through scripts/memory_delete_section.py, not by the
    model. Backs up first. Returns a one-line report."""
    path = Path(path)
    with memory_lock(path):
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        heads = [(i, ln) for i, ln in enumerate(lines) if ln.lstrip().startswith("#")]
        q = " ".join(heading.strip().lower().lstrip("#").split())
        matches = [(i, ln) for i, ln in heads if q and q in " ".join(ln.lower().lstrip("#").split())]
        if not matches:
            raise ValueError(f"No section matching '{heading}'. Headings: " + "; ".join(ln.strip() for _, ln in heads[:40]))
        if len(matches) > 1:
            raise ValueError(f"'{heading}' is ambiguous: " + "; ".join(ln.strip() for _, ln in matches))
        start, head_line = matches[0]
        level = len(head_line) - len(head_line.lstrip("#"))
        end = len(lines)
        for j, ln in heads:
            if j > start and len(ln) - len(ln.lstrip("#")) <= level:
                end = j
                break
        removed = "".join(lines[start:end])
        backup(path, text)
        atomic_text(path, "".join(lines[:start] + lines[end:]))
    return f"Deleted section '{head_line.strip()}' ({len(removed.splitlines())} lines); backup saved first."

