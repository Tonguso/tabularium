"""Provider-independent request cleanup. Never discard instructions or replay tools."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from agent_core.memory_store import atomic_text

# A conservative wire-size trigger, NOT a claim about any model's token window.
# Server context errors trigger further reduction; protected content is never cut.
REQUEST_BYTES = 300_000
ARCHIVE_MARKER = "[Archived tool output: "
PROTECTED_TOOLS = {"skill", "read_memory", "consult_advisor"}


def wire_size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def archive_preview(content, directory: Path, preview_chars: int = 8000) -> str:
    """Save the exact original before replacing it with an explicitly partial preview."""
    is_text = isinstance(content, str)
    full = content if is_text else json.dumps(content, ensure_ascii=False, indent=2)
    digest = sha256(full.encode("utf-8")).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (digest + (".txt" if is_text else ".json"))
    # Rewriting the same immutable value also repairs an incomplete earlier write.
    path.write_text(full, encoding="utf-8")
    visible = full if is_text else "\n".join(
        b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    if len(visible) > preview_chars:
        tail = preview_chars // 4
        visible = visible[:preview_chars-tail] + "\n[...middle omitted from preview...]\n" + visible[-tail:]
    return (
        f"{ARCHIVE_MARKER}{path}]\n"
        f"Full original: {len(full)} characters. This is a PARTIAL preview, not a summary. "
        "Use search_archived(query, path) to find the passage you need, then read_file(path, offset, limit) for it, before drawing conclusions. "
        "JSON archives retain image source blocks; execute_command can decode them for view_image. "
        "The original tool already ran; retrieve its record instead of repeating an action.\n"
        + visible)


def shorten_preview(content: str, preview_chars: int) -> str:
    """An already-archived preview (the fetch tool writes 8,000-char previews itself) keeps its header lines,
    which name the archive file, and loses the rest beyond preview_chars."""
    head, _, body = content.partition("\n")
    instr, _, visible = body.partition("\n")
    if len(visible) <= preview_chars:
        return content
    return head + "\n" + instr + "\n" + visible[:preview_chars] + "\n[preview shortened under context pressure; read_file the archive path above for more]"


def archive_input(value, directory: Path, preview_chars: int) -> dict:
    """Replace a bulky tool_use input (a whole script in write_file, say) with a pointer and a short preview."""
    full = json.dumps(value, ensure_ascii=False, indent=2)
    digest = sha256(full.encode("utf-8")).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (digest + ".json")
    path.write_text(full, encoding="utf-8")
    return {"archived_input": str(path), "preview": full[:preview_chars],
            "note": "Original tool input archived under context pressure; the call already ran. read_file the path for the full input."}


AGE_THRESHOLD = 1_500        # bytes; a stale tool result above this becomes a preview
AGE_PREVIEW = 400            # characters kept in the preview (the archive path carries the rest)


def compact_stale_results(messages: list, directory: Path, names: dict, protected_tail: int) -> list:
    """A fresh copy in which tool results (and bulky tool inputs) older than the protected tail are previews with
    an archive path. The canonical history is untouched; protected tools (skill, read_memory, consult_advisor)
    keep their full text because the model works from them for the whole task."""
    if len(messages) <= 2 + protected_tail:
        return messages
    result = deepcopy(messages)
    for message in result[2:len(result) - protected_tail]:
        if not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and message.get("role") == "assistant":
                if isinstance(block.get("input"), dict) and "archived_input" not in block["input"] and wire_size(block["input"]) >= 3_000:
                    block["input"] = archive_input(block["input"], directory, AGE_PREVIEW)
                continue
            if message.get("role") != "user" or block.get("type") != "tool_result":
                continue
            if names.get(block.get("tool_use_id")) in PROTECTED_TOOLS:
                continue
            content = block.get("content", "")
            if not isinstance(content, (str, list)) or wire_size(content) < AGE_THRESHOLD:
                continue
            if isinstance(content, str) and content.startswith(ARCHIVE_MARKER):
                block["content"] = shorten_preview(content, AGE_PREVIEW)
            else:
                block["content"] = archive_preview(content, directory, preview_chars=AGE_PREVIEW)
    return result


def prepare_context(messages: list, directory: Path, *, retry: bool = False,
                    budget_bytes: int = REQUEST_BYTES) -> list:
    """Keep all messages, requests, findings, tool calls and recent results.

    Only bulky older tool payloads become retrievable previews, only under pressure.
    Never mutate canonical history. Archive failure propagates before any request is sent.
    """
    names = {b.get("id"): b.get("name") for m in messages
             for b in (m.get("content") if isinstance(m.get("content"), list) else [])
             if isinstance(b, dict) and b.get("type") == "tool_use"}
    protected_tail = 2 if retry else 6
    size = wire_size(messages)
    if not retry and size <= budget_bytes:
        return messages  # Preserve the cached prefix until an actual context boundary.
    # Cut with room to grow, not on every next result. Keep recent results and protected instructions.
    target = min(budget_bytes, size // 2) if retry else int(budget_bytes * 0.6)
    messages = compact_stale_results(messages, directory, names, protected_tail)
    if wire_size(messages) <= target:
        return messages
    # Escalating passes, each from a fresh copy: archive every result at or above the threshold with that preview
    # size, then check the budget. The old single pass stopped at the first fit and kept 8,000-char previews, so a
    # 56-message worker history came out at 536 KB against a 160 KB budget and cost 144k tokens a turn (task 92,
    # 2026-10-07). Nothing here touches the canonical history; the full originals are on disk for read_file.
    result = messages
    for threshold, preview in ((12_000, 8000), (3_000, 1200), (1_500, 400), (600, 160)):
        result = deepcopy(messages)
        for message in result[2:max(2, len(result)-protected_tail)]:
            if not isinstance(message.get("content"), list):
                continue
            for block in message["content"]:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and message.get("role") == "assistant":
                    # Bulky inputs (a whole script echoed in write_file) cost as much as results do.
                    if isinstance(block.get("input"), dict) and "archived_input" not in block["input"] and wire_size(block["input"]) >= max(threshold, 3_000):
                        block["input"] = archive_input(block["input"], directory, preview)
                    continue
                if message.get("role") != "user" or block.get("type") != "tool_result":
                    continue
                if names.get(block.get("tool_use_id")) in PROTECTED_TOOLS:
                    continue
                content = block.get("content", "")
                if not isinstance(content, (str, list)):
                    continue
                if wire_size(content) < threshold:
                    continue
                if isinstance(content, str) and content.startswith(ARCHIVE_MARKER):
                    block["content"] = shorten_preview(content, preview)   # already archived: shorten, never skip
                else:
                    block["content"] = archive_preview(content, directory, preview_chars=preview)
        if wire_size(result) <= target:
            return result
    return result


def clear_cache_markers(messages: list) -> list:
    """Remove old request-boundary markers without changing tool input dictionaries."""
    result = deepcopy(messages)
    def scrub(blocks):
        if not isinstance(blocks, list):
            return
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block.pop("cache_control", None)
            if block.get("type") == "tool_result":
                scrub(block.get("content"))
    for message in result:
        scrub(message.get("content"))
    return result


def exchange_starts(messages):
    """User exchanges, not tool-result messages, are the unit of conversational retention."""
    starts, pending = [], set()
    for i, message in enumerate(messages[2:], 2):
        content = message.get('content')
        blocks = content if isinstance(content, list) else []
        if message.get('role') == 'user' and not pending:
            if isinstance(content, str):
                if not content.startswith(('[Local recovery record]', '[System:', '[Background scope update')):
                    starts.append(i)
            elif any(b.get('type') in ('text', 'image') for b in blocks):
                starts.append(i)
        for block in blocks:
            if block.get('type') == 'tool_use':
                pending.add(block.get('id'))
            elif block.get('type') == 'tool_result':
                pending.discard(block.get('tool_use_id'))
    return starts


def completed_exchanges(messages, directory):
    """Keep the conversation verbatim; archive completed tool traces once per exchange."""
    starts = exchange_starts(messages)
    if len(starts) < 2:
        return messages
    result = list(messages[:starts[0]])
    for start, end in zip(starts, starts[1:]):
        exchange = messages[start:end]
        protected = {b.get('id') for m in exchange for b in
                     (m.get('content') if isinstance(m.get('content'), list) else [])
                     if b.get('type') == 'tool_use' and b.get('name') in PROTECTED_TOOLS}
        uses = {b.get('id') for m in exchange for b in
                (m.get('content') if isinstance(m.get('content'), list) else []) if b.get('type') == 'tool_use'}
        results = {b.get('tool_use_id') for m in exchange for b in
                   (m.get('content') if isinstance(m.get('content'), list) else []) if b.get('type') == 'tool_result'}
        protected.update(uses ^ results)  # a follow-up may arrive between a call and its result
        trace, kept = [], []
        for message in exchange:
            content = message.get('content')
            if not isinstance(content, list):
                kept.append(message)
                continue
            visible, removed = [], []
            for block in content:
                tool = block.get('type') in ('tool_use', 'tool_result')
                ident = block.get('id') if block.get('type') == 'tool_use' else block.get('tool_use_id')
                (removed if tool and ident not in protected else visible).append(block)
            if removed:
                trace.append({**message, 'content': removed})
            if visible:
                kept.append({**message, 'content': visible})
        if trace:
            full = json.dumps(trace, ensure_ascii=False)
            path = directory / (sha256(full.encode('utf-8')).hexdigest() + '.json')
            directory.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                atomic_text(path, full)
            # This is evidence of past calls, not a fabricated user instruction or an instruction to run again.
            kept.insert(1, {'role': 'assistant', 'content':
                f'[Completed tool trace: {path}. Exact calls and results retained; read_file to inspect. Do not replay actions.]'})
        result.extend(kept)
    result.extend(messages[starts[-1]:])
    return result
