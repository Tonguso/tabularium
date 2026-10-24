"""Search inside archived tool output (2026-10-18).

Long tool results (a filing section, a fetched page, a report) are archived in full and the model sees a preview; until
now the only way back in was read_file with a guessed offset. search_archived ranks the paragraphs of one archive, or
of every archive in this runtime's context_archive, by the query's terms and returns the best few with the offset to
read_file from. Plain scoring over local files, no index to keep: an agent's archive is hundreds of files, not millions.
"""
import re
from pathlib import Path

from agent_core.outcome import Failed

SEARCH_ARCHIVED_TOOL = {
    "name": "search_archived",
    "description": ("Find passages inside archived tool output (the [Archived tool output: ...] files) by keyword, instead of "
                    "paging through read_file. Give the archive path to search one file, or leave it out to search every "
                    "archive of this session. Returns the best passages with the character offset to read_file from."),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Words or a phrase, English or Chinese."},
            "path": {"type": "string", "description": "One archive file to search; omit for all archives in this runtime."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "description": "Passages to return; default 6."},
        },
        "required": ["query"],
    },
}

CJK = r"[㐀-䶿一-鿿]"


def terms(query):
    """Lower-cased search terms: words, and Chinese runs split into overlapping bigrams (a run of two keeps itself)."""
    out = []
    for token in re.findall(r"[A-Za-z0-9_$%.&-]+|" + CJK + "+", query.lower()):
        if re.match(CJK, token):
            out += [token] if len(token) <= 2 else [token[i:i + 2] for i in range(len(token) - 1)]
        else:
            out.append(token.strip(".-"))
    return [t for t in dict.fromkeys(out) if t]


def passages(text, size=600):
    """(offset, passage) over paragraph boundaries where the text has them, fixed windows where it does not."""
    out, pos = [], 0
    for part in re.split(r"(\n\s*\n)", text):
        if part.strip() and not re.fullmatch(r"\s+", part):
            for i in range(0, len(part), size):
                out.append((pos + i, part[i:i + size]))
        pos += len(part)
    return out


def score(passage, words, phrase):
    """((distinct terms matched, whole phrase present, occurrences), matched terms): more of the query beats more of
    one word. Latin terms match at a word start, so 'demand' does not light up 'on-demand' less than 'demand'."""
    low = passage.lower()
    hits = [w for w in words if (re.search(r"(?<![a-z0-9])" + re.escape(w), low) if w[0].isascii() else w in low)]
    if not hits:
        return None
    return (len(hits), phrase in low, sum(low.count(w) for w in hits)), hits


def search(archive_dir, query, path="", limit=6, roots=()):
    """archive_dir: this runtime's archive, searched when no path is given. roots: other folders a path may point into
    (a finished background task's archive, named in task_evidence)."""
    words = terms(query)
    if not words:
        return Failed("search_archived: give at least one word.")
    archive_dir = Path(archive_dir)
    if path:
        file = Path(path)
        try:
            inside = any(file.resolve().is_relative_to(Path(r).resolve()) for r in (archive_dir, *roots))
        except OSError:
            inside = False
        if not inside or not file.is_file():
            return Failed(f"search_archived: {path} is not an archived tool output of this agent.")
        files = [file]
    else:
        files = sorted(archive_dir.rglob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)[:400]
    if not files:
        return "No archived tool output yet in this session."
    phrase = " ".join(query.lower().split())
    ranked = []
    for file in files:
        try:
            text = file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for offset, passage in passages(text):
            scored = score(passage, words, phrase)
            if scored:
                ranked.append((scored[0], scored[1], file, offset, passage))
    if not ranked:
        return f"No passage matches {', '.join(words)} in {len(files)} archive(s)."
    ranked.sort(key=lambda r: r[0], reverse=True)
    lines = [f"Best passages for {', '.join(words)} ({len(ranked)} matching in {len(files)} archive(s)); read_file(path, offset) for context:"]
    for s, hits, file, offset, passage in ranked[:int(limit or 6)]:
        snippet = " ".join(passage.split())
        lines.append(f"\n[{file} @ {offset}] matched {', '.join(hits)}\n{snippet[:500]}")
    return "\n".join(lines)
