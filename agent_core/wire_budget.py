"""One contract for tool payloads and Claude CLI frames, including its duplicated toolUseResult.

Sizes use JSON bytes (ASCII escaping is the conservative serializer), not Python characters
or raw image bytes. The CLI echoes each tool payload twice; transport must allow both copies.
"""
import json
from agent_core.context_window import archive_preview
from agent_core.outcome import Failed

IMAGE_ENCODED_CAP = 512 * 1024
MAX_IMAGES = 10
TEXT_HEADROOM = 512 * 1024
ENVELOPE_HEADROOM = 64 * 1024
CLI_RESULT_COPIES = 2


def message_budget():
    return CLI_RESULT_COPIES * (IMAGE_ENCODED_CAP * MAX_IMAGES + TEXT_HEADROOM) + ENVELOPE_HEADROOM


def encoded_size(raw_bytes):
    return 4 * ((raw_bytes + 2) // 3)


def json_size(value):
    return len(json.dumps(value, ensure_ascii=True, default=str).encode("ascii"))


def violation(value):
    images = []

    def text_only(item):
        if isinstance(item, dict):
            if item.get("type") == "image":
                data = item.get("source", item).get("data", "")
                images.append(len(data.encode("utf-8")))
                return {k: text_only(v) for k, v in item.items() if k not in ("source", "data")}
            return {k: text_only(v) for k, v in item.items()}
        if isinstance(item, list):
            return [text_only(v) for v in item]
        return item

    text = json_size(text_only(value))
    if len(images) > MAX_IMAGES:
        return f"{len(images)} images exceeds the {MAX_IMAGES}-image result limit"
    if any(size > IMAGE_ENCODED_CAP for size in images):
        return f"encoded image exceeds the {IMAGE_ENCODED_CAP}-byte image limit"
    if text > TEXT_HEADROOM:
        return f"serialized text is {text} bytes; limit is {TEXT_HEADROOM}"
    if json_size(value) > IMAGE_ENCODED_CAP * MAX_IMAGES + TEXT_HEADROOM:
        return "serialized result exceeds the total tool payload limit"
    return ""


def bound_result(value, directory):
    """The action already ran. Archive its exact result, return an explicit failure, never repeat it."""
    reason = violation(value)
    if not reason:
        return value
    prefix = f"Tool output rejected at the wire boundary: {reason}. The tool already ran; do not repeat its action. "
    try:
        preview = archive_preview(value, directory, preview_chars=4000)
    except Exception as exc:
        return Failed(prefix + f"Archiving also failed ({type(exc).__name__}); request a smaller result.")
    result = Failed(prefix + preview)
    if violation(result):
        return Failed(prefix + "Archive saved, but its preview also exceeded the budget; request a smaller result.")
    return result
