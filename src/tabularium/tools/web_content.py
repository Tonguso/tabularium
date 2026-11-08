"""Fetch representation: visible text with source identity, full archive, and optional targeted passages."""
from lxml import etree
from tabularium.core.context_window import archive_preview
from tabularium.memory.archive_search import search


def visible_text(content):
    doc = etree.fromstring(content, parser=etree.HTMLParser())
    if doc is None:
        return ""
    # These elements are not page prose. Preserve tails and all visible body/table text.
    etree.strip_elements(doc, "script", "style", "noscript", "template", with_tail=False)
    return "\n".join(part for text in doc.itertext() if (part := " ".join(text.split())))


def page_result(text, url, directory, query=""):
    source = f"Source: {url}\n"
    if len(text) <= 6000:
        return source + text
    preview = archive_preview(text, directory, preview_chars=1200)
    if not query.strip():
        return source + preview
    # The existing archive format gives the exact immutable path; no second index or memory store.
    path = preview.split("\n", 1)[0][len("[Archived tool output: "):-1]
    hits = search(directory, query, path=path, limit=4)
    return source + preview + "\n\nPassages matching the requested query:\n" + str(hits)
