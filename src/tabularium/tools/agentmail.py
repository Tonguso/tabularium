"""The agent's own mailbox: an AgentMail inbox (api.agentmail.to), reached with a key that is bound to that one inbox.

The key names the inbox (GET /auth/me), so nothing else is configured: <NAME>_AGENTMAIL_API_KEY in the home's settings
gives that agent its email tools, and an agent without a key has none. The owner makes the inbox and its key in the
AgentMail console; the runtime never creates inboxes.

Mail goes out as HTML with a plain-text alternative. The agents write Markdown-shaped text (paragraphs, **bold**, lists);
`html()` renders exactly that, so a mail reads as written instead of as flowed plain text (2026-11-13: months of mail
from the plain-text bridge arrived with its paragraphs run together)."""
import base64
import html as _html
import mimetypes
from pathlib import Path
import re

import requests

BASE = "https://api.agentmail.to/v0"
TIMEOUT = 60


class AgentMail:
    def __init__(self, key, base=BASE):
        self._headers = {"Authorization": "Bearer " + key}
        self._base = base
        self.inbox = None            # the inbox the key is bound to, learnt on first use

    def _call(self, method, path, **kw):
        r = requests.request(method, self._base + path, headers=self._headers, timeout=TIMEOUT, **kw)
        if r.status_code >= 400:
            try:
                err = r.json()
                detail = err.get("fix") or err.get("message") or r.text
                raise RuntimeError(f"AgentMail {r.status_code} {err.get('code', '')}: {detail}")
            except ValueError:
                raise RuntimeError(f"AgentMail {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    def address(self):
        """The inbox address the key is bound to; the same string serves as inbox_id in paths."""
        if self.inbox is None:
            me = self._call("GET", "/auth/me")
            self.inbox = me.get("inbox_id") or me.get("scope_id")
            if not self.inbox:
                raise RuntimeError("The AgentMail key is not bound to an inbox (scope " + str(me.get("scope_type")) + ").")
        return self.inbox

    def messages(self, limit=10, sender="", subject=""):
        params = {"limit": limit}
        if sender:
            params["from"] = sender
        if subject:
            params["subject"] = subject
        return self._call("GET", f"/inboxes/{self.address()}/messages", params=params).get("messages", [])

    def message(self, message_id):
        return self._call("GET", f"/inboxes/{self.address()}/messages/{message_id}")

    def send(self, to, subject, text, cc=(), attachments=(), reply_to_id=""):
        """Returns {"message_id", "thread_id"}. A reply (reply_to_id) threads under that message."""
        body = {"to": list(to), "subject": subject, "text": text, "html": html(text)}
        if cc:
            body["cc"] = list(cc)
        if attachments:
            body["attachments"] = [attachment(p) for p in attachments]
        if reply_to_id:
            body.pop("subject")
            return self._call("POST", f"/inboxes/{self.address()}/messages/{reply_to_id}/reply", json=body)
        return self._call("POST", f"/inboxes/{self.address()}/messages/send", json=body)


def attachment(path):
    p = Path(path)
    return {"filename": p.name, "content_type": mimetypes.guess_type(p.name)[0] or "application/octet-stream",
            "content": base64.b64encode(p.read_bytes()).decode()}


def addresses(text):
    """'a@x; b@y, c@z' -> ['a@x', 'b@y', 'c@z']."""
    return [a.strip() for a in re.split(r"[;,]", text or "") if a.strip()]


def _inline(text):
    text = _html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', text)


def html(text):
    """Markdown-shaped text to simple HTML: blank lines separate paragraphs, lines starting with '- ' or '* ' or
    '1. ' make lists, '# ' headings, line breaks inside a paragraph are kept."""
    out = []
    for block in re.split(r"\n\s*\n", text.strip().replace("\r\n", "\n")):
        lines = block.split("\n")
        if all(re.match(r"\s*[-*•]\s+", l) for l in lines):
            out.append("<ul>" + "".join(f"<li>{_inline(re.sub(r'^\s*[-*•]\s+', '', l))}</li>" for l in lines) + "</ul>")
        elif all(re.match(r"\s*\d+[.)]\s+", l) for l in lines):
            out.append("<ol>" + "".join(f"<li>{_inline(re.sub(r'^\s*\d+[.)]\s+', '', l))}</li>" for l in lines) + "</ol>")
        elif len(lines) == 1 and re.match(r"#{1,3}\s+", lines[0]):
            level = len(lines[0]) - len(lines[0].lstrip("#"))
            out.append(f"<h{level}>{_inline(lines[0].lstrip('#').strip())}</h{level}>")
        else:
            out.append("<p>" + "<br>".join(_inline(l) for l in lines) + "</p>")
    return ('<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;font-size:15px;line-height:1.5">'
            + "".join(out) + "</div>")
