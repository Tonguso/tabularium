"""Requirement checks: execute the declared test, retain evidence, derive status from current bytes.

A passing command proves its assertions only. Choosing a test that exercises the requirement is
still the reviewer's responsibility; reports and subjective judgments are never automated passes.
"""
import hashlib
import json
from pathlib import Path
import time
from agent_runtime import atomic_json
from agent_core.outcome import Failed, Refused
from agent_core.worker_lease import WorkerLease

SPEC = {"type": "object", "properties": {
    "kind": {"type": "string", "enum": ["command", "contains"]},
    "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1,
              "description": "Absolute delivered file first, then test scripts and dependencies whose changes invalidate proof."},
    "argv": {"type": "array", "items": {"type": "string"}, "minItems": 2, "description": "Executable and arguments for one assertion-based test. Include {artifact} as an entire argument. Test must exit nonzero on failure; include test scripts in paths."},
    "text": {"type": "string", "description": "contains only: exact required text in a UTF-8 file (up to 36000 characters)."}},
    "required": ["kind", "paths"]}
ITEM = {"type": "object", "properties": {"item": {"type": "string"}, "check": {"type": "string"},
        "id": {"type": "integer", "minimum": 1, "description": "Stable requirement number: existing number updates its check; the next unused number appends a new obligation. Omission also appends or matches by exact item and source."},
        "source": {"type": "integer", "minimum": 0, "description": "Own task: 0 for the original brief, or the exact added instruction ID shown in the task contract. Every instruction needs its own obligation; never reassign an old one."},
        "critical": {"type": "boolean"}, "early": {"type": "boolean"}, "verification": SPEC},
        "required": ["item", "check", "critical"]}
TOOL = {"name": "verify_requirement", "description": "Ensure a declared requirement has current proof. Reuse passing proof when the requirement and file hashes are unchanged; otherwise execute its check. Declare completion_items in task_checkpoint (own work) or delegate submit/rescope. No command or pass/fail claims are accepted here. Passes prove only the declared test; subjective/report-only items stay unverified. rerun=true explicitly requests fresh execution; a failed or interrupted run replaces earlier proof. Uses existing tool permissions.",
        "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"},
            "request_id": {"type": "string", "description": "Owner-side delegation request; omit for own task."},
            "item": {"type": "integer", "minimum": 1},
            "rerun": {"type": "boolean", "description": "Force fresh execution only when explicitly needed despite unchanged requirement and files."}}, "required": ["task_id", "item"]}}


def validate_items(items):
    if not isinstance(items, list) or not items:
        raise ValueError("completion_items must be a nonempty list.")
    for item in items:
        if not isinstance(item, dict) or not item.get("item") or not item.get("check"):
            raise ValueError("Each requirement needs item and check.")
        spec = item.get("verification")
        if spec is None:
            continue                            # judgment/report, not executable proof
        if not isinstance(spec, dict) or spec.get("kind") not in ("command", "contains"):
            raise ValueError("verification.kind must be command or contains.")
        paths = spec.get("paths")
        if not isinstance(paths, list) or not paths or any(not isinstance(p, str) or not Path(p).is_absolute() for p in paths):
            raise ValueError("verification.paths needs absolute artifact and dependency paths.")
        if spec["kind"] == "command":
            argv = spec.get("argv")
            if not isinstance(argv, list) or len(argv) < 2 or any(not isinstance(arg, str) for arg in argv) or "{artifact}" not in argv[1:] or not argv[0]:
                raise ValueError("The check needs argv: executable and arguments including {artifact}.")
        if spec["kind"] == "contains" and not spec.get("text"):
            raise ValueError("A contains check needs nonempty text.")


def scope(store, task):
    """Requirements belong to the exact user brief and delivered scope updates."""
    value = [store.get(task)["request"], store.scope_updates(task)]
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def delegated_items(items):
    return [dict({k: v for k, v in item.items() if k not in ("source", "id")},
                 source=0, parent_source=item.get("source", 0), parent_item=item.get("id", n))
            for n, item in enumerate(items, 1)]


def merge_items(previous, updates):
    """Stable positions and obligations; omitted items remain, test details can be revised."""
    validate_items(updates)
    result = [dict(item) for item in previous]
    for update in updates:
        value = dict(update)
        number = value.pop("id", None)
        source = value.get("source", 0)
        if type(source) is not int or source < 0:
            raise ValueError("Requirement source must be 0 (brief) or a delivered instruction ID.")
        if number is None:
            number = next((n for n, old in enumerate(result, 1)
                           if old["item"] == value["item"] and old.get("source", 0) == source), None)
        if type(number) is int and number == len(result) + 1:
            result.append(value)
            continue
        if number is not None:
            if type(number) is not int or not 1 <= number <= len(result):
                raise ValueError("Unknown requirement ID.")
            old = result[number - 1]
            if value["item"] != old["item"] or source != old.get("source", 0):
                raise ValueError("An existing obligation cannot be renamed or assigned to a different instruction. Add a new item.")
            # Priority is not permission to remove a requirement from completion.
            result[number - 1] = dict(old, **value)
        else:
            result.append(value)
    return result


def saved_items(store, task, request=""):
    """Read the one durable contract, migrating an older contract once on first use."""
    import sqlite3
    db = sqlite3.connect(store.database, timeout=30)
    try:
        row = db.execute("SELECT items FROM requirements WHERE task=? AND request=?", (task, request)).fetchone()
        if row is not None:
            return json.loads(row[0])
        if request:
            old = db.execute("SELECT items FROM scorecard WHERE task=? AND request_id=?", (task, request)).fetchone()
            items = json.loads(old[0]) if old and old[0] else []
        else:
            path = store.directory(task) / "checkpoint.json"
            old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            context = store.directory(task) / "context.json"
            ctx = json.loads(context.read_text(encoding="utf-8")) if context.exists() else {}
            items = (ctx.get("contract") or {}).get("completion_items", [])
            if old.get("completion_items"):
                items = merge_items(items, old["completion_items"])
        db.execute("INSERT OR IGNORE INTO requirements(task,request,items) VALUES (?,?,?)",
                   (task, request, json.dumps(items, ensure_ascii=False)))
        db.commit()
        return json.loads(db.execute("SELECT items FROM requirements WHERE task=? AND request=?", (task, request)).fetchone()[0])
    finally:
        db.close()


def declare(store, task, updates, request=""):
    """Merge a checkpoint's test declarations into task-owned obligations, never replace them."""
    import sqlite3
    saved_items(store, task, request)
    db = sqlite3.connect(store.database, timeout=30)
    try:
        db.execute("BEGIN IMMEDIATE")
        owner = db.execute("SELECT attempt FROM tasks WHERE id=?", (task,)).fetchone()
        if store.worker_attempt and (not owner or owner[0] != store.worker_attempt):
            raise ValueError("Worker no longer owns this task.")
        old = json.loads(db.execute("SELECT items FROM requirements WHERE task=? AND request=?", (task, request)).fetchone()[0])
        if not request:
            sources = {0} | {r[0] for r in db.execute("SELECT id FROM controls WHERE task=? AND applied=1", (task,))}
            if any(it.get("source", 0) not in sources for it in updates):
                raise ValueError("Requirement source must identify this task's brief or a delivered instruction.")
        items = merge_items(old, updates)
        db.execute("UPDATE requirements SET items=? WHERE task=? AND request=?", (json.dumps(items, ensure_ascii=False), task, request))
        db.commit()
        return items
    finally:
        db.close()


def uncovered_sources(store, task, items):
    sources = {0} | {update["id"] for update in store.scope_updates(task)}
    return sorted(sources - {item.get("source", 0) for item in items})


def contract(store, task, request=""):
    if request:
        card = store.scorecard(request)
        if not card or int(card["task"]) != int(task):
            raise ValueError("The request does not belong to this task.")
        return saved_items(store, task, request), card.get("reworks", 0)
    return saved_items(store, task), scope(store, task)


def key(store, task, request, item):
    # A new obligation does not change what an existing test proved. Completion separately
    # requires every instruction to be covered and every requirement to pass.
    source = item.get("source", 0)
    instruction = store.get(task)["request"] if not source else next(
        (update["text"] for update in store.scope_updates(task) if update["id"] == source), None)
    if not request and instruction is None:
        raise ValueError("Requirement instruction is missing.")
    return hashlib.sha256(json.dumps([item, instruction], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def receipt_path(store, task, request, item):
    scope = hashlib.sha256(request.encode()).hexdigest()[:24] if request else "own"
    return store.directory(task) / "verification" / scope / f"{item}.json"


def hashes(paths):
    result = {}
    for name in paths:
        path = Path(name).resolve()
        stream = path.open("rb")
        try:
            result[str(path)] = hashlib.file_digest(stream, "sha256").hexdigest()
        finally:
            stream.close()
    return result


def status(store, task, request, item):
    items, generation = contract(store, task, request)
    try:
        record = json.loads(receipt_path(store, task, request, item).read_text(encoding="utf-8"))
        spec = items[item - 1].get("verification")
        if not isinstance(record, dict):
            return "unverified"
        if not spec:
            return "unverified"
        if record.get("task") != task or record.get("request") != request or record.get("item") != item:
            return "unverified"
        if record.get("hashes") != hashes(spec["paths"]):
            return "unverified"
        current = key(store, task, request, items[item - 1])
        if "requirement" not in record:
            # One-way format migration, only while the complete old proof is still valid.
            legacy = hashlib.sha256(json.dumps([items, generation], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if record.get("contract") != legacy:
                return "unverified"
            path = receipt_path(store, task, request, item)
            lease = WorkerLease(path.with_suffix(".lock"))
            try:
                if not lease.acquire() or json.loads(path.read_text(encoding="utf-8")) != record:
                    return "unverified"
                record.pop("contract")
                record["requirement"] = current
                atomic_json(path, record)
            finally:
                lease.close()
        if record.get("requirement") != current:
            return "unverified"
        return record["status"] if record.get("status") in ("passed", "failed") else "unverified"
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return "unverified"


def execute(agent, chat, task_id, item, request_id="", rerun=False):
    store = agent._background
    task = int(task_id)
    if store.get(task)["chat"] != chat or (agent.BACKGROUND_TASK_ID and int(agent.BACKGROUND_TASK_ID) != task):
        return Refused("Verification belongs to another chat or worker.")
    lease = None
    try:
        items, generation = contract(store, task, request_id)
        validate_items(items)
        if type(item) is not int or not 1 <= item <= len(items):
            raise ValueError("Unknown requirement number.")
        spec = items[item - 1].get("verification")
        if not spec:
            raise ValueError("No executable check declared; this requirement is unverified.")
        if request_id and store.delegation_returned_at(request_id, task) is None:
            raise ValueError("Wait for the delivered result before verifying it.")
        path = receipt_path(store, task, request_id, item)
        if not rerun and status(store, task, request_id, item) == "passed":
            return json.dumps({"item": item, "status": "passed", "reused": True, "record": str(path)}, ensure_ascii=False)
        lease = WorkerLease(path.with_suffix(".lock"))
        if not lease.acquire():
            return Refused("This requirement check is already running.")
        record = {"task": task, "request": request_id, "item": item, "requirement": key(store, task, request_id, items[item - 1]),
                  "started": time.time(), "status": "unverified"}
        atomic_json(path, record)                       # crash or refusal must invalidate an old pass
        tool = "execute_command" if spec["kind"] == "command" else "read_file"
        available = {t["name"] for t in agent.get_tools("anthropic")}
        if tool not in available:
            return Refused(f"Verification requires {tool}, which this agent cannot use.")
        if agent.BACKGROUND_TASK_ID and agent.config.background_refusals:
            for name in spec["paths"]:
                refusal = store.path_refusal(task, name)
                if refusal:
                    return Refused(refusal)
        record["hashes"] = hashes(spec["paths"])
        if tool == "execute_command":
            argv = [str(Path(spec["paths"][0]).resolve()) if arg == "{artifact}" else arg for arg in spec["argv"]]
            quoted = " ".join("'" + arg.replace("'", "''") + "'" for arg in argv)
            inputs = {"command": "$ErrorActionPreference='Stop'; $global:LASTEXITCODE=0; & " + quoted +
                      "; if (-not $?) { exit 1 }; exit $LASTEXITCODE"}
        else:
            inputs = {"path": spec["paths"][0], "limit": 36000}
        result = agent._execute_tool_unguarded(tool, inputs, chat)
        record["tool"] = tool
        record["inputs"] = inputs
        record["output"] = str(result)
        if not isinstance(result, (Failed, Refused)):
            if tool == "execute_command":
                outcome = json.loads(result)
                passed = outcome.get("exit") == 0 and outcome.get("passed") is True and outcome.get("timed_out") is False
            else:
                # Do not treat a paging header or a partial read as proof of file contents.
                text = Path(spec["paths"][0]).read_text(encoding="utf-8")
                passed = len(text) <= 36000 and result == text and spec["text"] in text
            record["status"] = "passed" if passed else "failed"
        elif isinstance(result, Failed):
            record["status"] = "failed"
        if record["hashes"] != hashes(spec["paths"]):
            record.update(status="unverified", reason="Artifact or test dependency changed during verification.")
        if record["requirement"] != key(store, task, request_id, saved_items(store, task, request_id)[item - 1]):
            record.update(status="unverified", reason="Requirement changed during verification.")
        atomic_json(path, record)
        return json.dumps({"item": item, "status": record["status"], "record": str(path),
                           "scope": items[item - 1]["check"]}, ensure_ascii=False)
    except (OSError, ValueError, TypeError) as exc:
        return Failed(f"Verification unverified: {exc}")
    finally:
        if lease is not None:
            lease.close()


def verdicts(store, task, request, proposed):
    items, _ = contract(store, task, request)
    validate_items(items)
    seen, normalized = set(), []
    for value in proposed or []:
        if not isinstance(value, dict):
            raise ValueError("Each verdict must be an object.")
        number = value.get("item")
        if type(number) is not int or not 1 <= number <= len(items) or number in seen:
            raise ValueError("Each verdict needs a distinct requirement number.")
        seen.add(number)
        state = status(store, task, request, number)
        if value.get("how") in ("ran", "read") and state == "unverified":
            raise ValueError(f"Item {number} has no current requirement-bound proof. Run verify_requirement or label it report/judgment (unverified).")
        result = dict(value, status=state, assessment=value.get("assessment", value.get("evidence", "")),
                      verdict="met" if state == "passed" else "not_met" if state == "failed" else "unverified")
        if state != "unverified":
            result["how"] = "ran" if items[number - 1]["verification"]["kind"] == "command" else "read"
            result["evidence"] = str(receipt_path(store, task, request, number))
        normalized.append(result)
    if any(it.get("critical") and n not in seen for n, it in enumerate(items, 1)):
        raise ValueError("Every critical requirement needs a verdict.")
    return normalized


def summary(store, task, request=""):
    items, _ = contract(store, task, request)
    if not items:
        return "Verification: unverified (no requirements declared)."
    groups = {"passed": [], "failed": [], "unverified": []}
    for n, it in enumerate(items, 1):
        groups[status(store, task, request, n)].append(f"{n} {it['item']}")
    return "Verification: " + "; ".join(f"{state}: {', '.join(names)}" for state, names in groups.items() if names) + "."
