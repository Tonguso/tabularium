"""Persistent original images and a bounded visual working context."""
import base64
from contextlib import closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from agent_runtime import atomic_json
from agent_core.memory_store import memory_lock

RECENT_TURNS = 10
RECENT_IMAGES = 20

class ImageMemory:
    def __init__(self, agent_dir):
        self.root = Path(agent_dir) / "attachments" / "images"
        self.root.mkdir(parents=True, exist_ok=True)
        self.agent_dir = Path(agent_dir)

    def store(self, chat, data, media_type, caption="", sent=None, kind="photo"):
        directory = self.root / str(int(chat))
        directory.mkdir(exist_ok=True)
        digest = hashlib.sha256(data).hexdigest()
        suffix = {"image/jpeg":".jpg","image/png":".png","image/gif":".gif","image/webp":".webp"}.get(media_type,".img")
        path = directory / (digest + suffix)
        if not path.exists():
            fd, temporary = tempfile.mkstemp(dir=directory,suffix=".tmp")
            try:
                with os.fdopen(fd,"wb") as stream:
                    stream.write(data); stream.flush(); os.fsync(stream.fileno())
                os.replace(temporary,path)
            finally:
                Path(temporary).unlink(missing_ok=True)
        index = directory / "index.json"
        with memory_lock(index):
            rows = json.loads(index.read_text(encoding="utf-8")) if index.exists() else []
            existing = next((r for r in rows if r["id"] == digest and r["kind"] == kind),None)
            if existing:
                changed = False
                if caption and not existing.get("caption"):
                    existing["caption"] = caption[:1500]
                    changed = True
                if sent and not existing.get("sent"):
                    existing["sent"] = sent
                    changed = True
                if changed:
                    atomic_json(index,rows)
                return existing
            row = dict(id=digest,path=str(path),media_type=media_type,kind=kind,
                       caption=caption[:1500],sent=sent,stored_at=time.time())
            rows.append(row)
            atomic_json(index,rows)
            return row

    def list(self, chat, query="", limit=12, offset=0, include_views=False):
        index = self.root / str(int(chat)) / "index.json"
        if not index.exists():
            return []
        rows = json.loads(index.read_text(encoding="utf-8"))
        rows = [r for r in rows if (include_views or r["kind"]=="photo")
                and (not query or query.casefold() in (r["caption"]+" "+r["id"]).casefold())]
        rows.sort(key=lambda r:r.get("sent") or r["stored_at"],reverse=True)
        start=max(0,int(offset)); rows=rows[start:start+min(30,max(1,int(limit)))]
        return [{**r,"date":datetime.fromtimestamp(r.get("sent") or r["stored_at"]).isoformat(timespec="minutes"),
                 "date_basis":"Telegram sent time" if r.get("sent") else "local archival time; original send time unknown"}
                for r in rows]

    def retain(self, messages, chat):
        """Count human turns, not assistant/tool messages. Archive before replacing pixels."""
        age=-1
        kept=set()
        for message in reversed(messages):
            content=message.get("content")
            # Keep one current catalogue, not a repeated copy on every historic turn.
            marker = "[Available images (local originals)"
            if message.get("role") == "user":
                if isinstance(content, str):
                    content = content.split("\n\n" + marker, 1)[0]
                elif isinstance(content, list):
                    content = [b for b in content if not (
                        isinstance(b, dict) and b.get("type") == "text"
                        and b.get("text", "").startswith(marker))]
                message["content"] = content
            is_result=isinstance(content,list) and any(isinstance(b,dict) and b.get("type")=="tool_result" for b in content)
            if message.get("role")=="user" and not is_result:
                age+=1
            if not isinstance(content,list):
                continue
            caption="\n".join(b.get("text","") for b in content if isinstance(b,dict) and b.get("type")=="text")[:1500]
            def scrub(blocks,kind):
                result=[]
                for block in blocks:
                    if not isinstance(block,dict):
                        result.append(block);continue
                    if block.get("type")=="image" and block.get("source",{}).get("type")=="base64":
                        source=block["source"]
                        data=base64.b64decode(source["data"],validate=True)
                        row=self.store(chat,data,source.get("media_type","image/jpeg"),caption,kind=kind)
                        keep = (age < RECENT_TURNS if kind=="photo" else age < 1)
                        keep = keep and (len(kept)<RECENT_IMAGES or row["id"] in kept or age==0)
                        if keep:
                            result.append(block);kept.add(row["id"])
                        else:
                            result.append({"type":"text","text":f"[Saved {kind}: {row['path']}. Original retained; use view_image to inspect it again. Image ID {row['id'][:16]}]"})
                    elif block.get("type")=="tool_result" and isinstance(block.get("content"),list):
                        result.append({**block,"content":scrub(block["content"],"view")})
                    else:
                        result.append(block)
                return result
            message["content"]=scrub(content,"photo")

    def recover_telegram(self, bot, chat):
        """Recover already-stripped recent uploads from the durable inbox, without resending."""
        database=self.agent_dir/"task_state"/"inbox.sqlite3"
        if not database.exists():
            return 0
        with closing(sqlite3.connect(f"file:{database.as_posix()}?mode=ro",uri=True)) as db:
            rows=db.execute("SELECT message,payload FROM jobs WHERE chat=? ORDER BY id DESC", (int(chat),)).fetchall()
        directory=self.root/str(int(chat))
        directory.mkdir(exist_ok=True)
        receipts=directory/"telegram_recovered.json"
        done=json.loads(receipts.read_text()) if receipts.exists() else []
        restored=0
        candidates=0
        for ident,payload in rows:
            raw=json.loads(payload)
            if not raw.get("photo"):
                continue
            candidates+=1
            if candidates>30:
                break
            if ident in done:
                continue
            try:
                info=bot.get_file(raw["photo"][-1]["file_id"])
                data=bot.download_file(info.file_path)
                media="image/png" if data.startswith(b"\x89PNG") else "image/jpeg"
                self.store(chat,data,media,raw.get("caption") or "",raw.get("date"),kind="photo")
                done.append(ident);atomic_json(receipts,done)
                restored+=1
            except Exception as exc:
                print("[image-memory] Telegram recovery pending:",type(exc).__name__)
        return restored

    def catalog(self, chat):
        rows=self.list(chat,limit=12)
        if not rows:
            return ""
        lines=["[Available images (local originals). Use view_image on the relevant path when pixels are absent; do not ask Alex to resend a saved photo."]
        for row in rows:
            lines.append(f"{row['date']} | {row['id'][:16]} | {row['path']} | {row['caption'][:140]}")
        lines.append("Use list_images for more uploads. Dates may be archival time; check date_basis.]")
        return "\n".join(lines)
