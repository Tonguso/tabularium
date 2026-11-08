"""Every message to Alex is durable until Telegram accepts it (2026-10-21).

Around 22:11-22:14 this PC lost its route to api.telegram.org for a few minutes. Cato's reply to Alex's 22:11 Tiro
message failed once and was dropped: chat replies had one attempt, and only background results were retried. That
broke the rule that nothing important waits in memory. Now each chunk of an outgoing message is written to
<agent>/task_state/outbox.sqlite3 before it is sent, then sent at once as before. A network failure (no route,
timeout, Telegram 429/5xx) leaves it queued, and a retry thread delivers queued chunks per chat in order once the
connection is back; a chunk delivered more than two minutes late says when it was written. A rejection by Telegram
itself (a quote of a deleted message, bad formatting) falls back to no quote, then plain text, as before; if even
that is refused the chunk is marked failed and logged, never retried forever.

2026-10-22 21:47: one reply stalled for minutes (a 502, then read timeouts on every retry) while curl from the same
PC reached api.telegram.org in 0.1 s and polling still received Alex's messages. The stuck part was the bot's
own pooled HTTPS connection, the "poisoned connection pool" the polling loop already resets after a crash. The
retry loop now calls `reset` (the engine passes the pool reset) before every retry that follows a transient
failure, so a dead keep-alive socket costs one retry interval, not the evening.
"""
import re
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

LATE_SECONDS = 120
RETRY_SECONDS = (5, 15, 30, 60)


def transient(exc):
    """A failure worth retrying later: the network, or Telegram saying 'not now' (429, 5xx)."""
    try:
        from telebot.apihelper import ApiTelegramException
    except ImportError:                       # pragma: no cover - telebot is installed wherever agents run
        ApiTelegramException = ()
    if ApiTelegramException and isinstance(exc, ApiTelegramException):
        code = getattr(exc, "error_code", 0) or 0
        return code == 429 or code >= 500
    try:
        import requests
        if isinstance(exc, requests.exceptions.RequestException):
            return True
    except ImportError:                       # pragma: no cover
        pass
    return isinstance(exc, (OSError, TimeoutError, ConnectionError))


def plain(chunk):
    return re.sub(r"<[^>]+>", "", chunk).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


class Outbox:
    def __init__(self, directory, bot_getter, on_sent=None, reset=None):
        """directory: the agent's runtime folder. bot_getter: returns the Telegram bot at send time (tests replace
        it). on_sent(decision_id, message_id): records the Telegram id of a message that asked a decision.
        reset(): drops the bot's HTTP connection pool; called before a retry that follows a transient failure."""
        self.path = Path(directory) / "task_state" / "outbox.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.bot_getter, self.on_sent, self.reset = bot_getter, on_sent, reset
        self.lock = threading.Lock()
        self.wake = threading.Event()
        with closing(self._db()) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY, chat INTEGER, text TEXT, reply_to INTEGER, "
                       "decision INTEGER, first INTEGER, status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0, "
                       "created REAL, sent REAL, message_id INTEGER, error TEXT)")

        db = self._db()
        try:
            db.execute('BEGIN IMMEDIATE')
            columns = {row[1] for row in db.execute('PRAGMA table_info(outbox)')}
            for name, declaration in [('channel', "TEXT NOT NULL DEFAULT 'telegram'"), ('original', 'TEXT')]:
                if name not in columns:
                    db.execute(f'ALTER TABLE outbox ADD COLUMN {name} {declaration}')
            db.commit()
        finally:
            db.close()

    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def put(self, chat, chunks, reply_to=None, decision=0, channel='telegram', original=None):
        """Queue the chunks of one message, then try to deliver this chat's queue now. Returns the Telegram ids of
        the chunks delivered in this call (empty when the connection is down: they stay queued)."""
        if channel not in ('telegram', 'dashboard'):
            raise ValueError('Unknown delivery channel')
        now = time.time()
        db = self._db()
        try:
            for i, chunk in enumerate(c for c in chunks if c):
                db.execute("INSERT INTO outbox(chat,text,reply_to,decision,first,created,channel,original,status) VALUES (?,?,?,?,?,?,?,?,?)",
                    (chat, chunk, reply_to if i == 0 else None, decision or 0, int(i == 0), now, channel,
                     original if i == 0 or original is None else '',
                     'sent' if channel == 'dashboard' else 'pending'))
            db.commit()
        finally:
            db.close()
        return self.flush(chat) if channel == 'telegram' else []

    def pending(self, chat=None):
        with closing(self._db()) as db:
            q, args = "SELECT * FROM outbox WHERE status='pending'", []
            if chat is not None:
                q += " AND chat=?"; args.append(chat)
            return [dict(r) for r in db.execute(q + " ORDER BY id", args)]

    def flush(self, chat=None):
        """Deliver queued chunks in order, per chat; stop a chat at its first network failure so order holds."""
        delivered = []
        with self.lock:
            stalled = set()
            for row in self.pending(chat):
                if row["chat"] in stalled:
                    continue
                text = row["text"]
                if row["first"] and time.time() - row["created"] > LATE_SECONDS:
                    text = f"[Written {time.strftime('%H:%M', time.localtime(row['created']))}; delayed by a connection outage]\n" + text
                try:
                    message_id = self._deliver(row["chat"], text, row["reply_to"])
                except Exception as exc:                    # noqa: BLE001 - classified below
                    if transient(exc):
                        stalled.add(row["chat"])
                        if not row["attempts"]:                # once per message: the log shows a stall as it starts
                            print(f"[outbox] message {row['id']} not accepted by Telegram, will retry: {type(exc).__name__}: {str(exc)[:160]}")
                        self._mark(row["id"], "pending", error=f"{type(exc).__name__}: {exc}", attempt=True)
                        continue
                    self._mark(row["id"], "failed", error=f"{type(exc).__name__}: {exc}", attempt=True)
                    print(f"[outbox] Telegram refused a message for good: {type(exc).__name__}: {exc}")
                    continue
                self._mark(row["id"], "sent", message_id=message_id)
                if isinstance(message_id, int):
                    delivered.append(message_id)
                    if row["decision"] and self.on_sent:
                        self.on_sent(row["decision"], message_id)
        return delivered

    def _deliver(self, chat, chunk, reply_to):
        """Quote, then without the quote (the quoted message may be gone), then plain text (malformed HTML). A network
        failure is raised at once: it is not the message's fault, and the queue will retry it."""
        bot = self.bot_getter()
        attempts = ([dict(text=chunk, parse_mode="HTML", reply_to_message_id=reply_to)] if reply_to else [])
        attempts += [dict(text=chunk, parse_mode="HTML"), dict(text=plain(chunk))]
        last = None
        for kw in attempts:
            try:
                sent = bot.send_message(chat, **kw)
                mid = getattr(sent, "message_id", None)
                return mid if isinstance(mid, int) else None
            except Exception as exc:                        # noqa: BLE001
                if transient(exc):
                    raise
                last = exc
        raise last

    def _mark(self, ident, status, message_id=None, error=None, attempt=False):
        with closing(self._db()) as db, db:
            db.execute("UPDATE outbox SET status=?, sent=CASE WHEN ?='sent' THEN ? ELSE sent END, message_id=COALESCE(?, message_id), "
                       "error=COALESCE(?, error), attempts=attempts+? WHERE id=?",
                       (status, status, time.time(), message_id, error, 1 if attempt else 0, ident))

    def start(self):
        """The retry thread: waits, then flushes whatever is still queued, backing off while the network is down."""
        def loop():
            failures = 0
            while True:
                self.wake.wait(RETRY_SECONDS[min(failures, len(RETRY_SECONDS) - 1)])
                self.wake.clear()
                try:
                    if self.pending():
                        if failures and self.reset:
                            self.reset()                    # a dead keep-alive socket must not eat every retry
                        self.flush()
                        failures = failures + 1 if self.pending() else 0
                    else:
                        failures = 0
                except Exception as exc:                    # noqa: BLE001 - the thread must outlive any one error
                    print(f"[outbox] retry loop: {type(exc).__name__}: {exc}")
        threading.Thread(target=loop, name="outbox", daemon=True).start()
