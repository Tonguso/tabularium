"""Channel boundary and dashboard view over the existing durable inbox/outbox."""
import hashlib
import os
import re
import tempfile
import threading
import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace


MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
MAX_STORED_BYTES = 1024 * 1024 * 1024
MEDIA_EXTENSIONS = {
    'image': {'.jpg', '.jpeg', '.png', '.gif', '.webp'},
    'voice': {'.ogg', '.mp3', '.wav', '.m4a', '.weba'},
    'video': {'.mp4', '.mov', '.webm', '.mkv'},
    'document': {'.pdf', '.txt', '.docx', '.xlsx'},
}
UPLOAD_LOCK = threading.Lock()


def attachment_kind(filename):
    if not isinstance(filename, str) or not 1 <= len(filename) <= 180 or any(c in filename for c in '/\\:\x00'):
        raise ValueError('Use a simple filename without folders')
    for kind, extensions in MEDIA_EXTENSIONS.items():
        if Path(filename).suffix.lower() in extensions:
            return kind
    raise ValueError('Unsupported attachment type. Use an image, audio, video, PDF, text, Word or Excel file.')


def attachment_path(directory, ref):
    if not isinstance(ref, dict):
        raise ValueError('Invalid attachment')
    name, ident = ref.get('name'), ref.get('id', '')
    attachment_kind(name)
    if not isinstance(ident, str) or not re.fullmatch(r'[0-9a-f]{64}\.[a-z0-9]+', ident) or Path(ident).suffix != Path(name).suffix.lower():
        raise ValueError('Invalid attachment identity')
    folder = (Path(directory)/'media/dashboard').resolve()
    path = (folder/ident).resolve()
    if path.parent != folder or not path.is_file() or not 0 < path.stat().st_size <= MAX_ATTACHMENT_BYTES:
        raise ValueError('Attachment is missing; upload it again before sending')
    return path


def store_attachment(directory, filename, stream, size):
    kind = attachment_kind(filename)
    if type(size) is not int or not 0 < size <= MAX_ATTACHMENT_BYTES:
        raise ValueError('Choose a nonempty file no larger than 50 MB')
    if kind == 'voice' and size > 25*1024*1024:
        raise ValueError('Audio must be 25 MB or smaller for transcription')
    folder = Path(directory)/'media/dashboard'
    folder.mkdir(parents=True, exist_ok=True)
    UPLOAD_LOCK.acquire()
    temporary = None
    try:
        if sum(p.stat().st_size for p in folder.iterdir() if p.is_file()) + size > MAX_STORED_BYTES:
            raise ValueError('Saved uploads have reached 1 GB for this agent; archive them before uploading more')
        output = tempfile.NamedTemporaryFile(dir=folder, suffix='.part', delete=False)
        temporary = Path(output.name)
        digest, remaining = hashlib.sha256(), size
        try:
            while remaining:
                block = stream.read(min(1024*1024, remaining))
                if not block:
                    raise ValueError('Upload was interrupted; the message was not queued')
                output.write(block)
                digest.update(block)
                remaining -= len(block)
            output.flush()
            os.fsync(output.fileno())
        finally:
            output.close()
        ident = digest.hexdigest() + Path(filename).suffix.lower()
        target = folder/ident
        if not target.exists():
            os.replace(temporary, target)
        return {'id': ident, 'name': filename, 'size': size, 'kind': kind}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        UPLOAD_LOCK.release()


def ready(directory, media=False):
    from tabularium.ops.supervisor import process_created
    try:
        health = json.loads((Path(directory)/'task_state/health.json').read_text(encoding='utf-8'))
        born = process_created(health['pid'])
        return bool(health.get('messaging', 0) >= (2 if media else 1) and born and born <= health['last_progress']
                    and time.time() < max(health['last_progress']+150, health.get('deadline', 0))
                    and not (Path(directory)/'task_state/drain').exists())
    except (OSError, ValueError, KeyError):
        return False


def decode(payload):
    if payload.get('channel') != 'dashboard':
        from telebot.types import Message
        return Message.de_json(payload)
    from tabularium.channels.owner_telegram import OWNER_CHAT_ID, OWNER_USER_ID
    return SimpleNamespace(chat=SimpleNamespace(id=OWNER_CHAT_ID, type='private'),
        from_user=SimpleNamespace(id=OWNER_USER_ID), message_id=payload['message_id'],
        date=payload['date'], text=payload['text'], caption=payload['text'], media_group_id=None,
        attachment=payload.get('attachment'),
        reply_to_message=None, quote=None, channel='dashboard',
        decision_id=payload.get('decision_id', 0), json=payload)


def read_rows(path, query, args=()):
    if not path.exists():
        return []
    db = sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True, timeout=3)
    db.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in db.execute(query, args)]
    finally:
        db.close()


def conversation(directory, chat):
    folder = Path(directory) / 'task_state'
    incoming = read_rows(folder/'inbox.sqlite3',
        'SELECT id,message,payload,status,created FROM jobs WHERE chat=? ORDER BY id DESC LIMIT 100', (chat,))
    columns = read_rows(folder/'outbox.sqlite3', 'PRAGMA table_info(outbox)')
    first_only = ' AND (first=1 OR original IS NULL)' if any(c['name']=='original' for c in columns) else ''
    outgoing = read_rows(folder/'outbox.sqlite3',
        f'SELECT * FROM outbox WHERE chat=?{first_only} ORDER BY id DESC LIMIT 100', (chat,))
    rows = []
    for row in incoming:
        payload = json.loads(row['payload'])
        rows.append(dict(id=f"in:{row['id']}", role='user', text=payload.get('text') or payload.get('caption') or '[Attachment]',
            channel=payload.get('channel', 'telegram'), time=row['created'], status=row['status'],
            client_id=payload.get('client_id'), attachment=payload.get('attachment'), decision_id=payload.get('decision_id', 0)))
    for row in outgoing:
        if row.get('original') is None:
            from tabularium.records.outbox import plain
            body = plain(row['text'])
        elif row['first']:
            body = row['original']
        else:
            continue
        rows.append(dict(id=f"out:{row['id']}", role='assistant', text=body,
            channel=row.get('channel', 'telegram'), time=row['created'], status=row['status'], decision_id=row['decision'] or 0))
    rows.sort(key=lambda row: (row['time'], row['id']))
    return {'messages': rows, 'note': 'Recent saved messages. Older retained history is in Conversations.'}
