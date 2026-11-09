"""Requirement checks: execute the declared test, retain evidence, derive status from current bytes.

A passing command proves its assertions only. Choosing a test that exercises the requirement is
still the reviewer's responsibility; reports and subjective judgments are never automated passes.
"""
import hashlib
import json
from pathlib import Path
import time
from tabularium.core.agent_runtime import atomic_json
from tabularium.core.outcome import Failed, Refused
from tabularium.records.worker_lease import WorkerLease

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


def delegate(store, task, request):
    """Resolve the request to its authoritative worker task; no copied agreement."""
    import sqlite3
    from tabularium.records.background_runtime import TaskStore
    from tabularium.core.roster import ROLES
    db = sqlite3.connect(store.database, timeout=30)
    try:
        card = db.execute("SELECT task,agent FROM scorecard WHERE request_id=?", (request,)).fetchone()
    finally:
        db.close()
    if not card or int(card[0]) != int(task):
        raise ValueError("The request does not belong to this task.")
    name = {"vit": "engineer", "cal": "librarian"}.get(card[1])
    if not name:
        raise ValueError("Unknown delegate.")
    folder = store.agent_dir.parent / name
    database = folder / "task_state/background/jobs.sqlite3"
    if not database.exists():
        return None                       # historical unmapped contracts remain readable, never acceptable
    db = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=30)
    try:
        table = ROLES[name].request_table
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
            return None
        row = db.execute(f"SELECT task FROM {table} WHERE request_id=?", (request,)).fetchone()
    finally:
        db.close()
    if not row:
        return None
    remote = TaskStore(folder, ROLES[name])
    if remote.get(row[0])["chat"] != store.get(task)["chat"]:
        raise ValueError("Delegate belongs to another chat.")
    return remote, int(row[0])


def delivery(store, task, request):
    """Identity of the returned result and agreement, not the request's historical read flag."""
    import sqlite3
    resolved = delegate(store, task, request)
    if not resolved:
        raise ValueError("No mapped delegate task; inspect the legacy result before submitting current work.")
    remote, child = resolved
    state = remote.get(child)
    if state['status'] not in ('finished', 'needs_attention'):
        raise ValueError("The delegate has not returned a delivery for review.")
    db = sqlite3.connect(remote.database, timeout=30)
    try:
        controls = db.execute("SELECT id,text,applied FROM controls WHERE task=? ORDER BY id", (child,)).fetchall()
        if any(not row[2] for row in controls) or db.execute("SELECT 1 FROM decisions WHERE task=? AND state='pending'", (child,)).fetchone():
            raise ValueError("Delegate has unresolved instructions or decisions.")
    finally:
        db.close()
    items = saved_items(remote, child)
    paths = [str(remote.directory(child) / 'result.txt')]
    for item in items:
        paths.extend((item.get('verification') or {}).get('paths', []))
    card = store.scorecard(request)
    brief = json.loads(card.get('brief') or '{}')
    output = brief.get('output_path')
    if output:
        paths.append(output)
    # Missing declared artifacts remain reviewable as failures, never a passing check.
    artifacts = {str(Path(p).resolve()): hashes([p])[str(Path(p).resolve())] if Path(p).is_file() else None for p in paths}
    if artifacts[str(Path(paths[0]).resolve())] is None:
        raise ValueError("Delegate result is missing.")
    return dict(task=child, attempt=state.get('attempt'), updated=state['updated'],
                agreement=hashlib.sha256(json.dumps([items, controls], sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                artifacts=artifacts)


def review_record(store, task, request):
    if not task:
        raise ValueError("No owner task is attached to this request.")
    row = next((d for d in store.delegations(task) if d['request_id'] == request), None)
    if not row:
        raise ValueError('The request is not attached to this owner task.')
    value = json.loads(row.get('acceptance') or '{}')
    return value if isinstance(value, dict) else {}   # legacy read is not a delivery-bound review


def delivery_id(snapshot):
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()


def current_review(store, task, request):
    record = review_record(store, task, request)
    if not record.get('delivery') or record['delivery'] != delivery(store, task, request):
        raise ValueError("Fetch the current delegate result before reviewing it.")
    return record


def acceptance(store, task, request, proposed=None, observed=None):
    """One decision for both recording a review and completing its owner task."""
    record = current_review(store, task, request)
    if proposed is not None and observed != delivery_id(record['delivery']):
        raise ValueError('accept needs the delivery_id returned by the result you reviewed.')
    values = proposed if proposed is not None else record.get('verdicts')
    if not values:
        raise ValueError('Every requirement needs a review verdict.')
    items = saved_items(store, task, request)
    record['verdicts'] = verdicts(store, task, request, values)
    unmet = [v['item'] for v in record['verdicts'] if v['verdict'] != 'met']
    if record['delivery'] != delivery(store, task, request):
        raise ValueError('Delivery changed during review; fetch the current result.')
    return record, [n for n in unmet if items[n-1].get('critical')], unmet


REWORK_CAP = 3


def item_paths(items, number, result_path):
    """The artifacts behind one requirement: its declared paths, else the delegate's result text."""
    paths = (items[number - 1].get('verification') or {}).get('paths') or []
    return [str(Path(p).resolve()) for p in paths] or [str(Path(result_path).resolve())]


def rework_refusal(store, task, request, items):
    """Review has to converge (2026-10-27, after the Tiro reworks): three reworks on one request are the cap, and an
    item the current review accepted as met cannot be sent back again."""
    card = store.scorecard(request)
    if card and (card.get('reworks') or 0) >= REWORK_CAP:
        return (f"Request {request} has had {REWORK_CAP} reworks, the cap: a fourth round does not converge. Either accept "
                "the current delivery with the open items marked not_met, so Alex sees what is missing, or submit a new "
                "request with a changed brief and say in brief_lesson what this one got wrong.")
    if not task:
        return None
    try:
        record = current_review(store, task, request)
    except ValueError:
        return None                                   # no review of the current delivery: nothing to repeat
    met = {v['item'] for v in record.get('verdicts') or [] if v.get('verdict') == 'met'}
    repeated = sorted(set(items) & met)
    if repeated:
        return f"Items {repeated} were accepted as met on this delivery; a rework cannot flag them again. Send back only the open items."
    return None


def rework_snapshot(store, task, request, items):
    """Hashes of the artifacts behind the items sent back, so the next review can tell a change from a wave-off."""
    try:
        snapshot = delivery(store, task, request)
    except ValueError:
        return {}
    contract_items = saved_items(store, task, request)
    result_path = next(iter(snapshot['artifacts']))
    wanted = {p for n in items if 1 <= n <= len(contract_items) for p in item_paths(contract_items, n, result_path)}
    return {p: h for p, h in snapshot['artifacts'].items() if p in wanted}


def rework_outcome(entry, snapshot, items):
    """Per item sent back: changed (the delegate altered the artifact) or unchanged (the finding was waved off)."""
    result_path = next(iter(snapshot['artifacts']))
    return {str(n): 'changed' if any(snapshot['artifacts'].get(p) != entry.get('artifacts', {}).get(p)
                                     for p in item_paths(items, n, result_path)) else 'unchanged'
            for n in entry.get('items', []) if 1 <= n <= len(items)}


def cold_review(agent_dir, delivery_id):
    """The finished cold read (specialist_review role delivery) of this exact delivery, or None."""
    folder = Path(agent_dir) / 'reports' / 'specialist_reviews'
    if not delivery_id or not folder.exists():
        return None
    for path in sorted(folder.glob('*.json'), reverse=True):
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if record.get('role') == 'delivery' and record.get('delivery_id') == delivery_id and record.get('status') == 'finished':
            return str(path)
    return None


def transfer_proof(store, task, request, item, remote, child):
    """One-way reuse of unchanged pre-migration owner proof; never override a worker's newer check."""
    import sqlite3
    target = receipt_path(remote, child, '', item)
    if target.exists():
        return
    old_path = store.directory(task) / 'verification' / hashlib.sha256(request.encode()).hexdigest()[:24] / f'{item}.json'
    if not old_path.exists():
        return
    db = sqlite3.connect(store.database, timeout=30)
    try:
        row = db.execute('SELECT items FROM requirements WHERE task=? AND request=?', (task, request)).fetchone()
    finally:
        db.close()
    try:
        old = json.loads(row[0])[item-1] if row else None
        current = saved_items(remote, child)[item-1]
        record = json.loads(old_path.read_text(encoding='utf-8'))
        clean = lambda it: {k:v for k,v in it.items() if k not in ('id','source','parent_source','parent_item')}
        if not old or current.get('source', 0) != 0 or clean(old) != clean(current) or current.get('parent_source', 0) != old.get('source', 0):
            return
        source = old.get('source', 0)
        instruction = store.get(task)['request'] if not source else next((x['text'] for x in store.scope_updates(task) if x['id']==source), None)
        expected = hashlib.sha256(json.dumps([old, instruction], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if (record.get('task'),record.get('request'),record.get('item'),record.get('requirement')) != (task,request,item,expected):
            return
        if record.get('status') != 'passed' or record.get('hashes') != hashes(current['verification']['paths']):
            return
        lease = WorkerLease(target.with_suffix('.lock'))
        try:
            if lease.acquire() and not target.exists():
                record.update(task=child, request='', requirement=key(remote, child, '', current))
                atomic_json(target, record)
        finally:
            lease.close()
    except (ValueError, TypeError, KeyError, IndexError, OSError):
        return                             # incompatible historical evidence remains unverified


def saved_items(store, task, request=""):
    """Read the one durable contract, migrating an older contract once on first use."""
    import sqlite3
    if request:
        resolved = delegate(store, task, request)
        if resolved:
            return saved_items(*resolved)
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
    if request:
        resolved = delegate(store, task, request)
        if resolved:
            return declare(*resolved, updates)
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
    if request:
        resolved = delegate(store, task, request)
        if resolved:
            return receipt_path(*resolved, "", item)
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
    if request:
        resolved = delegate(store, task, request)
        if resolved:
            transfer_proof(store, task, request, item, *resolved)
            return status(*resolved, "", item)
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
    owner = store
    owner_task = task
    try:
        if request_id:
            delivery(store, task, request_id)
            resolved = delegate(store, task, request_id)
            if resolved:
                transfer_proof(store, task, request_id, item, *resolved)
                store, task = resolved
                request_id = ""
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
                refusal = owner.path_refusal(owner_task, name)
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
        if len(str(value.get('evidence', '')).strip()) < 20:
            raise ValueError(f'Item {number} needs evidence explaining the assessment.')
        if value.get('how') not in ('ran', 'read', 'report', 'judgment'):
            raise ValueError(f'Item {number} needs how: ran, read, report or judgment.')
        seen.add(number)
        state = status(store, task, request, number)
        subjective = is_judgment(items[number - 1])
        if not subjective and value.get("how") in ("ran", "read") and state == "unverified":
            raise ValueError(f"Item {number} has no current requirement-bound proof. Run verify_requirement or label it report/judgment (unverified).")
        review = value.get('verdict')
        if review not in ('met', 'partial', 'not_met', 'unverified'):
            raise ValueError('Unknown review verdict.')
        if subjective:
            outcome = review if value.get('how') in ('judgment', 'read') or review != 'met' else 'unverified'
        else:
            outcome = 'not_met' if review == 'not_met' or state == 'failed' else 'partial' if review == 'partial' else 'met' if state == 'passed' else 'unverified'
        result = dict(value, status=state, assessment=value.get("assessment", value.get("evidence", "")), verdict=outcome)
        if state != "unverified":
            result["how"] = "ran" if items[number - 1]["verification"]["kind"] == "command" else "read"
            result["evidence"] = str(receipt_path(store, task, request, number))
        normalized.append(result)
    if len(seen) != len(items):
        raise ValueError("Every requirement needs a review verdict.")
    return normalized


def is_judgment(item):
    """A requirement with no declared check is judged by the owner reading the result, not by the runtime."""
    return not (isinstance(item, dict) and item.get("verification"))


def summary(store, task, request=""):
    """What the owner needs to know about the declared checks, in plain words, or "" when there is nothing to say.

    2026-10-24 (Alex: [quote removed]): every research result arrived under "Completion not
    verified: Requirement 1 ...: unverified." because requirements no machine can check (a comparison, a conclusion)
    were reported as failures. Judgment requirements are now silent; the owner judges them by reading. Only a declared
    check that failed, or that was declared and never run on the delivered file, is mentioned."""
    items, _ = contract(store, task, request)
    failed, not_run = [], []
    for n, it in enumerate(items, 1):
        if is_judgment(it):
            continue
        state = status(store, task, request, n)
        if state == "failed":
            failed.append(it["item"])
        elif state != "passed":
            not_run.append(it["item"])
    lines = []
    if failed:
        lines.append("Check failed: " + "; ".join(failed) + ".")
    if not_run:
        lines.append("Not checked on the delivered file: " + "; ".join(not_run) + ".")
    return "\n".join(lines)
