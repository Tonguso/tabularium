"""Durable Telegram inbox (DurableInbox), atomic JSON writes, tool-history recovery and the everyday-style prompt,
shared by all four agents and the dashboard. The agent runtime itself is engine.AgentRuntime."""
from tabularium.ops import release
import functools
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import uuid


def everyday_style_prompt(agent_dir):
    """Read this agent's local compact core; missing installation fails explicitly."""
    source = Path(agent_dir) / "skills" / "talk-like-a-human-being" / "runtime-core.md"
    return "\n\n" + source.read_text(encoding="utf-8").strip() + "\n"


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        # Windows readers can temporarily deny replacement. Preserve atomic writes.
        for attempt in range(6):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.02 * (2 ** attempt))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


FOLLOW_UP_SECONDS = 3     # bubbles Alex types this close together are one message (2026-10-16)


class DurableInbox:
    """Every Telegram message is a job. One runs at a time per agent. A text message that arrives while a turn is running
    is not a new task: it continues that turn (follow_ups, taken by engine._background_boundary at the next tool
    boundary), and bubbles typed within FOLLOW_UP_SECONDS of each other are one message (gather)."""

    def __init__(self, directory, send, bypass=None):
        self.directory = Path(directory) / 'task_state'
        self.directory.mkdir(exist_ok=True)
        self.database = self.directory / 'inbox.sqlite3'
        self.send, self.bypass = send, bypass
        self.handlers = {}
        self.wake = threading.Event()
        self.shutdown = threading.Event()
        self.local = threading.local()
        self.active_chat = None
        self.active_job = None
        self.health_lock = threading.Lock()
        self.health = {"pid": os.getpid(), "phase": "starting", "task": None,
                       "instance": os.environ.get('AGENT_LAUNCH_ID') or uuid.uuid4().hex, "release": release.LOADED, "messaging": 2}
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY, chat INTEGER, '
                       'message INTEGER, handler TEXT, payload TEXT, status TEXT, created REAL, '
                       'UNIQUE(chat, message))')
            db.execute('CREATE TABLE IF NOT EXISTS usage_events (id INTEGER PRIMARY KEY, job INTEGER, created REAL, kind TEXT, provider TEXT, model TEXT, billing TEXT, role TEXT, metrics TEXT)')
            db.execute('CREATE INDEX IF NOT EXISTS usage_job ON usage_events(job)')

    def record_event(self, kind, model='', billing='', provider='', usage=None, role='worker', details=None, event_id=None, partial=False):
        """Best-effort accounting. Never let a metrics failure interrupt the task."""
        from tabularium.models.usage import normalize, FIELDS
        payload = normalize(provider, usage) if usage is not None else {}
        if partial:
            known = {k: v for k, v in payload.items() if k in FIELDS and v is not None}
            if 'input_total' not in known and any(k in known for k in FIELDS[1:4]):
                known['input_total'] = sum(known.get(k, 0) for k in FIELDS[1:4])
            payload = dict.fromkeys(FIELDS) | {'known_subtotals': known}
        if details is not None:
            payload['details'] = details
        db = None
        try:
            db = sqlite3.connect(self.database, timeout=30)
            if event_id is None:
                saved_id = db.execute('INSERT INTO usage_events(job,created,kind,provider,model,billing,role,metrics) VALUES (?,?,?,?,?,?,?,?)',
                           ((getattr(self.local, 'job', None) or self.active_job), time.time(), kind, provider, model, billing, role, json.dumps(payload))).lastrowid
            else:
                saved_id = event_id
                changed = db.execute('UPDATE usage_events SET metrics=?,created=? WHERE id=? AND kind=? AND provider=? AND model=? AND role=?',
                                     (json.dumps(payload), time.time(), event_id, kind, provider, model, role)).rowcount
                if not changed:
                    raise ValueError('Usage snapshot no longer exists')
            db.commit()
            return saved_id
        except Exception as exc:
            print(f'[usage] Could not save accounting: {type(exc).__name__}')
            return event_id
        finally:
            if db is not None:
                db.close()


    def usage_summary(self, chat):
        with self.db() as db:
            row = db.execute('SELECT j.id FROM jobs j JOIN usage_events u ON u.job=j.id WHERE j.chat=? ORDER BY j.id DESC LIMIT 1', (chat,)).fetchone()
            if not row:
                return 'No per-task usage recorded yet. Tracking starts after the updated agent is restarted.'
            rows = db.execute('SELECT kind,provider,model,billing,role,metrics FROM usage_events WHERE job=? ORDER BY id', row).fetchall()
        from tabularium.models.usage import decode
        groups = {}
        tools = sum(r[0] == 'tool_started' for r in rows)
        for kind, provider, model, billing, role, raw in rows:
            if kind not in ('attempt', 'response'):
                continue
            key = (model, billing, role)
            group = groups.setdefault(key, {'attempt': 0, 'response': 0, 'metrics': []})
            group[kind] += 1
            if kind == 'response':
                group['metrics'].append(decode(raw, role))
        from tabularium.models.usage import aggregate, FIELDS
        lines = [f'Task #{row[0]}: {tools} tool actions started.']
        for (model, billing, role), group in groups.items():
            lines.append(f'{model} | {billing} | {role}: {group["attempt"]} application attempts, {group["response"]} responses.')
            totals = aggregate(group['metrics'])
            if not group['metrics'] or (all(totals[k] is None for k in FIELDS) and not any(totals['known_subtotals'].values())):
                lines.append('Token usage unavailable.')
                continue
            lines.append(', '.join(f'{key}={totals[key]:,}' if totals[key] is not None else
                                   f'{key}=unknown (known subtotal {totals["known_subtotals"][key]:,})' for key in FIELDS))
        lines.append('Language-model accounting only. SDK-internal retries may be hidden; missing usage is unknown, not zero. Provider-reported tokens, not subscription allowance. No dollar or quota estimate.')
        return '\n'.join(lines)


    def progress(self, phase, deadline_seconds=0, restart=None):
        with self.health_lock:
            now = time.time()
            if restart is not None:
                self.health['restart'] = restart
            self.health.update(phase=phase, last_progress=now, deadline=now+deadline_seconds,
                               task=(getattr(self.local, 'job', None) or self.active_job), chat=self.active_chat)
            try:
                atomic_json(self.directory / 'health.json', self.health)
            except OSError as exc:
                # Health telemetry must never kill message processing. Durable receipts
                # still fail closed if their own write cannot be completed.
                if now - getattr(self, '_health_write_warning', 0) >= 60:
                    print(f'[health] Status write failed; inbox continues: {type(exc).__name__}')
                    self._health_write_warning = now

    def db(self):
        # Connections are short-lived; FULL synchronous transactions survive process exit.
        from contextlib import contextmanager
        @contextmanager
        def connection():
            db = sqlite3.connect(self.database, timeout=30)
            try:
                with db:
                    yield db
            finally:
                db.close()
        return connection()

    def notify(self, chat, text):
        from tabularium.channels.owner_telegram import OWNER_CHAT_ID
        if chat != OWNER_CHAT_ID:
            return
        try:
            self.send(chat, text)
        except Exception as exc:
            print(f'[inbox] Notification failed: {type(exc).__name__}')

    def handler(self, fn=None, *, album=False):
        if fn is None:
            return lambda f: self.handler(f, album=album)
        self.handlers[fn.__name__] = (fn, album)
        @functools.wraps(fn)
        def accept(message):
            from tabularium.channels.owner_telegram import is_owner
            if not is_owner(message):
                return
            if self.bypass and self.bypass(message):
                return
            chat = message.chat.id
            group = getattr(message, 'media_group_id', None)
            with self.db() as db:
                waiting = db.execute("SELECT count(*) FROM jobs WHERE status IN ('pending','running')").fetchone()[0]
                # An album is one request: its later items join the first one's job (run) and get no notice of their own
                # (2026-11-02: seven photos, one answer, and a "queued behind them" notice for each photo).
                sibling = group and db.execute("SELECT count(*) FROM jobs WHERE chat=? AND status IN ('pending','running') "
                                               "AND json_extract(payload,'$.media_group_id')=?", (chat, str(group))).fetchone()[0]
                cursor = db.execute('INSERT OR IGNORE INTO jobs '
                                    '(chat,message,handler,payload,status,created) VALUES (?,?,?,?,?,?)',
                                    (chat, message.message_id, fn.__name__, json.dumps(message.json), 'pending', time.time()))
                added = cursor.rowcount
            if added:
                atomic_json(self.directory / 'requests' / f'{chat}_{message.message_id}.json', message.json)
                if waiting >= 2 and not sibling:     # one running and one already waiting: this is third in line, say so once
                    self.notify(chat, 'Still on your earlier messages; this one is queued behind them. /stop cancels current and queued work.')
                self.wake.set()
        return accept

    def accept_dashboard(self, client_id, text, decision_id=0, attachment=None):
        """Authenticated dashboard ingress. Signed IDs reserve legacy positive Telegram references."""
        from tabularium.channels.owner_telegram import OWNER_CHAT_ID
        if not isinstance(client_id, str) or not 1 <= len(client_id) <= 100:
            raise ValueError('Invalid message identity')
        if not isinstance(text, str) or (not text.strip() and attachment is None) or len(text) > 4000:
            raise ValueError('Write a message of 1-4000 characters')
        if type(decision_id) is not int or decision_id < 0:
            raise ValueError('Invalid question identity')
        if attachment is not None:
            from tabularium.channels.messaging import attachment_path, attachment_kind
            path = attachment_path(self.directory.parent, attachment)
            attachment = {'id': path.name, 'name': attachment['name'], 'size': path.stat().st_size,
                          'kind': attachment_kind(attachment['name'])}
            if decision_id:
                raise ValueError('Answer the question with text first, then send the attachment separately')
        db = sqlite3.connect(self.database, timeout=30)
        try:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT id,message,payload FROM jobs WHERE chat=? AND json_extract(payload,'$.channel')='dashboard' AND json_extract(payload,'$.client_id')=?", (OWNER_CHAT_ID, client_id)).fetchone()
            if row:
                old = json.loads(row[2])
                if old['text'] != text or old.get('decision_id', 0) != decision_id or old.get('attachment') != attachment:
                    raise ValueError('Message identity already used for different content')
                return {'job': row[0], 'message_id': row[1]}
            now = time.time()
            payload = {'channel': 'dashboard', 'client_id': client_id, 'text': text,
                       'date': now, 'decision_id': decision_id, 'attachment': attachment}
            cursor = db.execute('INSERT INTO jobs(chat,message,handler,payload,status,created) VALUES (?,NULL,?,?,?,?)',
                (OWNER_CHAT_ID, 'handle_dashboard_media' if attachment else ('handle_dashboard_reply' if decision_id else 'handle_message'), '{}', 'pending', now))
            ident = cursor.lastrowid
            payload['message_id'] = -ident
            db.execute('UPDATE jobs SET message=?,payload=? WHERE id=?', (-ident, json.dumps(payload), ident))
            db.commit()
            self.wake.set()
            return {'job': ident, 'message_id': -ident}
        finally:
            db.close()

    def destination(self, chat, message_id=None):
        message_id = message_id or self.answering()
        db = sqlite3.connect(self.database, timeout=3)
        try:
            row = db.execute('SELECT payload FROM jobs WHERE chat=? AND message=?', (chat, message_id)).fetchone()
            return json.loads(row[0]).get('channel', 'telegram') if row else 'telegram'
        finally:
            db.close()

    def answering(self):
        """The Telegram message id the running job answers, or None outside a job."""
        return getattr(self.local, 'message_id', None)

    def turn_messages(self):
        """[(message_id, text)] of Alex's messages the running turn has taken in: its own, then follow-ups."""
        return list(getattr(self.local, 'turn_messages', None) or [])

    def turn_sources(self, chat):
        """Real inbox payloads selected for this turn, including exact Telegram reply references."""
        db = sqlite3.connect(self.database, timeout=30)
        try:
            sources = []
            for ident, text in self.turn_messages():
                row = db.execute('SELECT payload FROM jobs WHERE chat=? AND message=?', (chat, ident)).fetchone()
                if row:
                    source = json.loads(row[0])
                    source['text'] = text
                    sources.append(source)
            return sources
        finally:
            db.close()

    def superseded(self, chat, message_id):
        """True when Alex has sent `chat` another message after `message_id`: a reply then quotes what it answers,
        as Alex does himself when several threads run at once (2026-10-20). Otherwise the reply follows his
        latest message and a quote would be noise."""
        with self.db() as db:
            return db.execute("SELECT 1 FROM jobs WHERE chat=? AND id>(SELECT id FROM jobs WHERE chat=? AND message=?) LIMIT 1", (chat, chat, message_id)).fetchone() is not None

    def follow_ups(self, chat, job):
        """Pending text messages Alex sent to `chat` after job `job` started, oldest first, as (id, Message)."""
        from tabularium.channels.messaging import decode
        from tabularium.channels.owner_telegram import is_owner
        with self.db() as db:
            rows = db.execute("SELECT id,payload FROM jobs WHERE chat=? AND handler='handle_message' AND status='pending' "
                              "AND id>? ORDER BY id", (chat, job)).fetchall()
        out = []
        for ident, raw in rows:
            message = decode(json.loads(raw))
            if is_owner(message) and message.text:
                out.append((ident, message))
        return out

    def absorb(self, ids):
        """Follow-ups a turn has taken and saved to history: finished by that turn, never run on their own."""
        if ids:
            with self.db() as db:
                db.executemany("UPDATE jobs SET status='done' WHERE id=? AND status='pending'", [(i,) for i in ids])

    def gather(self, chat, ident, created, message):
        """Wait until FOLLOW_UP_SECONDS have passed since the first bubble, then fold the bubbles sent meanwhile into it.
        Returns (job ids, message with the joined text)."""
        time.sleep(max(0.0, FOLLOW_UP_SECONDS - (time.time() - created)))
        extra = self.follow_ups(chat, ident)
        self.local.last_bubble = extra[-1][1].message_id if extra else None
        self.local.turn_messages = [(message.message_id, message.text)] + [(m.message_id, m.text) for _, m in extra]
        if extra:
            message.text = "\n".join([message.text] + [m.text for _, m in extra])
        return [ident] + [i for i, _ in extra], message

    def cancel(self, chat):
        with self.db() as db:
            count = db.execute("UPDATE jobs SET status='cancelled' WHERE chat=? AND status IN ('pending','held')", (chat,)).rowcount
        return count

    def fail_current(self):
        self.local.failed = True

    def recovery_note(self, chat):
        with self.db() as db:
            # Only the last 24 hours: a failure from five days ago is history, not a task to continue. Until the
            # 2026-10-07 audit every session start listed 14 of them back to 2026-10-02.
            rows = db.execute("SELECT id,message,status,payload FROM jobs WHERE chat=? AND status IN ('interrupted','held','failed') "
                              "AND created > ? ORDER BY id DESC LIMIT 5", (chat, time.time() - 86400)).fetchall()
        if not rows:
            return ''
        lines = ['Saved tasks from an earlier interruption. Do not automatically repeat external actions. Ask for direction if the current request does not specify what to continue.']
        for ident, message, status, payload in rows:
            path = self.directory / 'requests' / f'{chat}_{message}.json'
            if not path.exists():
                atomic_json(path, json.loads(payload))
            lines.append(f'Task #{ident}, {status}: original Telegram request at {path}')
        lines.append(f'Per-tool outcome records are in {self.directory / "actions"}. A started record without a finished result has an UNKNOWN outcome, not a failed action.')
        return '\n'.join(lines)

    def status(self, chat):
        with self.db() as db:
            rows = db.execute("SELECT id,status,handler,payload FROM jobs WHERE chat=? AND status NOT IN ('done','cancelled') ORDER BY id DESC LIMIT 10", (chat,)).fetchall()
        lines = []
        for i, status, handler, payload in rows:
            raw = json.loads(payload)
            text = raw.get('text') or raw.get('caption') or handler
            lines.append(f'#{i}: {status} - {text[:160]}')
        return '\n'.join(lines) or 'No pending or interrupted tasks.'

    def tool(self, fn):
        @functools.wraps(fn)
        def execute(name, inputs, chat_id=None):
            job = (getattr(self.local, 'job', None) or self.active_job)
            path = self.directory / 'actions' / f'{time.time_ns()}.json'
            record = {'job': job, 'chat': chat_id, 'tool': name, 'inputs': inputs,
                      'tool_use_id': getattr(self.local, 'tool_id', None),
                      'status': 'started', 'warning': 'If no result exists, outcome is unknown. Check before repeating an external action.'}
            atomic_json(path, record)  # Failure here prevents the action from running.
            self.record_event('tool_started', model=name)
            self.progress('tool:' + name, deadline_seconds=2100)
            try:
                result = fn(name, inputs, chat_id)
            except Exception as exc:
                record.update(status='error', error=str(exc))
                atomic_json(path, record)
                raise
            record.update(status='finished', result=result)
            atomic_json(path, record)
            self.progress('tool_finished:' + name)
            return result
        return execute

    def close(self):
        self.shutdown.set()
        self.wake.set()
        worker = getattr(self, 'worker', None)
        if worker:
            worker.join(timeout=5)

    def start(self):
        with self.db() as db:
            chats = [r[0] for r in db.execute("SELECT DISTINCT chat FROM jobs WHERE status='running'")]
            db.execute("UPDATE jobs SET status='interrupted' WHERE status='running'")
            for chat in chats:
                db.execute("UPDATE jobs SET status='held' WHERE chat=? AND status='pending'", (chat,))
        for chat in chats:
            self.notify(chat, 'A task was interrupted by shutdown. Its request and tool records are saved. I have held older queued messages to avoid replaying actions. Use /pending to see them, then tell me what to continue.')
        self.worker = threading.Thread(target=self.run, daemon=True, name='durable-inbox')
        self.worker.start()

    def run(self):
        from tabularium.core.shared_handlers import FileTooBig      # local: agent_core imports this module
        from tabularium.channels.messaging import decode
        while not self.shutdown.is_set():
            self.wake.wait(1)
            if self.shutdown.is_set():
                return
            self.wake.clear()
            with self.db() as db:
                row = db.execute("SELECT id,chat,handler,payload,created FROM jobs WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
            if not row:
                self.progress('idle')
                continue
            ident, chat, name, payload, created = row
            ids = [ident]
            try:
                fn, album = self.handlers['handle_message' if name == 'handle_dashboard_reply' else name]
                message = decode(json.loads(payload))
                from tabularium.channels.owner_telegram import is_owner
                if not is_owner(message):
                    with self.db() as db:
                        db.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (ident,))
                    continue
                batch = [message]
                if album and message.media_group_id:
                    time.sleep(2)  # Telegram sends album items as separate updates.
                    with self.db() as db:
                        for jid, raw in db.execute("SELECT id,payload FROM jobs WHERE chat=? AND handler=? AND status='pending' AND id<>? ORDER BY id", (chat, name, ident)):
                            other = decode(json.loads(raw))
                            if is_owner(other) and other.media_group_id == message.media_group_id:
                                ids.append(jid)
                                batch.append(other)
                self.local.message_id = message.message_id      # what this job answers; replies quote it when needed
                self.local.turn_messages = [(m.message_id, m.text or m.caption or '') for m in batch]
                if name == 'handle_message' and message.text:
                    ids, message = self.gather(chat, ident, created, message)
                    # Execution is coalesced; each original message keeps its identity.
                marks = ','.join('?' for _ in ids)
                with self.db() as db:
                    # /stop may have cancelled the job during the album collection wait.
                    claimed = db.execute(f"UPDATE jobs SET status='running' WHERE id IN ({marks}) AND status='pending'", ids).rowcount
                if claimed != len(ids):
                    continue
                self.active_chat = chat
                self.active_job = ident
                self.local.job, self.local.failed = ident, False
                self.progress('task_started', deadline_seconds=2100)
                fn(batch if album else message)
                status = 'failed' if self.local.failed else 'done'
            except FileTooBig as exc:                           # Alex's to fix, said plainly; not a runtime failure
                status = 'done'
                self.notify(chat, str(exc))
            except Exception as exc:
                status = 'failed'
                print(f'[inbox] Task #{ident} failed: {type(exc).__name__}: {exc}')
                self.notify(chat, f'Task #{ident} stopped with an error. Its request is saved; it was not automatically replayed.')
            finally:
                self.active_chat = None
                self.active_job = None
                self.local.job = None
                self.local.turn_messages = []
                self.progress('idle')
            with self.db() as db:
                db.executemany('UPDATE jobs SET status=? WHERE id=? AND status IN (\'running\',\'pending\')', [(status, i) for i in ids])


def recover_tool_history(messages, directory):
    """Repair interrupted exchanges from recorded results; never execute a tool here."""
    from copy import deepcopy
    messages = deepcopy(messages)
    directory = Path(directory) / 'task_state' / 'actions'
    records = {}
    for path in sorted(directory.glob('*.json')):
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
            if record.get('tool_use_id'):
                records[record['tool_use_id']] = (record, path)
        except (ValueError, OSError):
            continue
    out = []
    known = set()
    for index, message in enumerate(messages):
        blocks = message.get('content')
        if isinstance(blocks, list):
            # An old saved file may begin halfway through an exchange.
            blocks = [b for b in blocks if not isinstance(b, dict) or b.get('type') != 'tool_result' or b.get('tool_use_id') in known]
            if not blocks:
                continue
            message = {**message, 'content': blocks}
        out.append(message)
        if message.get('role') != 'assistant' or not isinstance(blocks, list):
            continue
        calls = [b for b in blocks if isinstance(b, dict) and b.get('type') == 'tool_use']
        known.update(b['id'] for b in calls)
        following = messages[index+1].get('content') if index+1 < len(messages) else None
        present = {b.get('tool_use_id') for b in following if isinstance(b, dict) and b.get('type') == 'tool_result'} if isinstance(following, list) else set()
        missing = []
        for call in calls:
            if call['id'] in present:
                continue
            record, path = records.get(call['id'], ({}, None))
            if record.get('status') == 'finished':
                content = record['result']
            else:
                content = ('Execution was interrupted. Outcome is UNKNOWN: inspect the saved action record '
                           f'{path or directory} and the actual external state before retrying. '
                           'Do not assume the operation failed or replay it automatically.')
            missing.append({'type': 'tool_result', 'tool_use_id': call['id'], 'content': content})
        if missing:
            # Keep all results together when some members of a parallel batch were saved.
            if present:
                messages[index+1] = {**messages[index+1], 'content': missing + following}
            else:
                out.append({'role': 'user', 'content': missing})
    return out
