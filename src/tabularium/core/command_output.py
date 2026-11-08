"""Command status is evidence; verbose stdout/stderr are retrievable artifacts, not conversation history."""
from hashlib import sha256
import json
import re
from tabularium.core.outcome import Failed

INLINE_CHARS = 4000
EXCERPT_CHARS = 2200
DIAGNOSTIC = re.compile(r"(?i)(\b(?:error|failed|failure|traceback|fatal|warning|exception)\b|^FAIL:|^E\s+)")


def excerpt(text):
    """Explicit excerpts, not a claimed summary. Keep diagnostic windows, plus start/end context."""
    if len(text) <= EXCERPT_CHARS:
        return text
    spans = [(0, 300), (max(0, len(text) - 500), len(text))]
    pos = 0
    for line in text.splitlines(keepends=True):
        if DIAGNOSTIC.search(line):
            spans.append((max(0, pos - 120), min(len(text), pos + len(line) + 240)))
        pos += len(line)
    chosen = []
    remaining = EXCERPT_CHARS - 800
    # Diagnostic coverage is bounded; full evidence is always retained and searchable.
    for start, end in spans[2:]:
        if remaining <= 0:
            break
        end = min(end, start + remaining)
        chosen.append((start, end)); remaining -= end - start
    merged = []
    for start, end in sorted(spans[:2] + chosen):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return "\n[...excerpt gap...]\n".join(f"[offset {start}]\n" + text[start:end] for start, end in merged)


def command_result(stdout, stderr, exit_code, directory, *, timed_out=False):
    """Do not infer success from log prose; keep the exit status and both streams separate."""
    result = {"exit": exit_code, "passed": exit_code == 0 and not timed_out, "timed_out": timed_out}
    if len(stdout) + len(stderr) <= INLINE_CHARS:
        result.update(stdout=stdout, stderr=stderr)
    else:
        directory.mkdir(parents=True, exist_ok=True)
        full = "STDOUT:\n" + stdout + "\nSTDERR:\n" + stderr
        path = directory / (sha256(full.encode("utf-8")).hexdigest() + ".txt")
        path.write_text(full, encoding="utf-8")
        result.update(archive=str(path), characters=len(full), excerpt=excerpt(full),
                      notice="Partial excerpts, not a summary. search_archived(query, path=archive) or read_file(path=archive, offset, limit) for full evidence. The command already ran; do not rerun it just to retrieve output.")
    text = json.dumps(result, ensure_ascii=False)
    return text if result["passed"] else Failed(text)
