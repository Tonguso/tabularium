"""The owner's slash commands and uploaded documents.
"""
import io
from datetime import datetime
from pathlib import Path
from tabularium.memory.memory_context import WorkingMemory


class OwnerCommands:
    """Part of AgentApp (tabularium.core.agent_app): methods moved here unchanged on 2026-11-24."""

    def handle_start(self, message):
        chat_id = message.chat.id
        self.conversations[chat_id] = self.load_memory_context()
        self._conversation_path(chat_id).unlink(missing_ok=True)
        self.bot.send_message(chat_id, self.config.start_reply)

    def handle_help(self, message):
        self.bot.send_message(message.chat.id, self.config.help_text)

    def handle_voice_cmd(self, message):
        chat_id = message.chat.id
        parts = message.text.strip().split(maxsplit=1)
        labels = {
            "auto": "auto (I speak when you send a voice note)",
            "on":   "on (I speak every reply)",
            "off":  "off (text only)",
        }
        if len(parts) == 1:
            if not self._tts_key():
                self.bot.send_message(chat_id, f"Voice needs {self._tts_key_name()} in .env. Once set: /voice on · off · auto")
                return
            self.bot.send_message(chat_id, f"Voice replies: {labels[self.get_voice_mode(chat_id)]}\nChange with: /voice on · off · auto")
            return
        arg = parts[1].strip().lower()
        if arg not in ("auto", "on", "off"):
            self.bot.send_message(chat_id, "Usage: /voice on · off · auto")
            return
        self.set_voice_mode(chat_id, arg)
        self.bot.send_message(chat_id, f"Voice replies set to {labels[arg]}.")

    def handle_threads_cmd(self, message):
        """/threads — show working memory. /threads pin|drop <name> — override the agent's judgement."""
        chat_id = message.chat.id
        if self.config.threads_stub:
            self.send_message(chat_id, self.config.threads_stub)
            return
        parts = message.text.strip().split(maxsplit=2)
        try:
            working = WorkingMemory(self.AGENT_DIR)
            current = working.snapshot()
        except Exception as e:
            self.bot.send_message(chat_id, f"Couldn't read memory: {e}")
            return
        threads, blocked = current['threads'], current['blocked']

        if len(parts) >= 3 and parts[1].lower() in ("pin", "unpin", "drop"):
            action, target = parts[1].lower(), parts[2].strip()
            hit = next((t for t in threads if target.lower() in t["name"].lower()), None)
            if not hit:
                self.bot.send_message(chat_id, f"No thread matching '{target}'. /threads to list them.")
                return
            if action == "drop":
                out = self.tool_work_thread(name=hit["name"], done=True, owner_asked=True)
            else:
                out = self.tool_work_thread(name=hit["name"], state=hit["state"],
                                            pinned=(action == "pin"), owner_asked=(action == "pin"))
                if action == "unpin":       # re-render without the pin
                    working.unpin(hit['name'])
                out = f"{action}ned '{hit['name']}'." if action == "pin" else f"Unpinned '{hit['name']}'."
            self.bot.send_message(chat_id, out.split(" Do not mention")[0])
            return

        if not threads:
            self.bot.send_message(chat_id,
                                  f"No open threads yet. Tell {self.config.display} to track something, "
                                  "or he'll start them himself as work comes up.")
            return
        today = datetime.now().date()
        lines = [f"Open threads ({len(threads)}/{self.OPEN_THREADS_MAX}):"]
        for t in threads:
            try:
                age = (today - datetime.strptime(t["date"], "%Y-%m-%d").date()).days
            except Exception:
                age = 0
            mark = "📌 " if t["pinned"] else ""
            old = f"  ⚠️ {age}d" if age >= self.THREAD_STALE_DAYS else (f"  {age}d" if age else "  today")
            lines.append(f"{mark}<b>{t['name']}</b>{old}\n   {t['state']}")
        if blocked:
            lines.append(f"\nNot tracking (your call): {', '.join(sorted(blocked))}")
        lines.append("\n/threads pin &lt;name&gt; · /threads unpin &lt;name&gt; · /threads drop &lt;name&gt;")
        self.bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML")

    def handle_auto_cmd(self, message):
        chat_id = message.chat.id
        if self.config.auto_stub:
            self.send_message(chat_id, self.config.auto_stub)
            return
        parts = message.text.strip().split(maxsplit=1)
        if len(parts) == 1:
            state = ("ON — I run every command without asking, including risky ones" if self.get_auto_mode(chat_id)
                     else "OFF (default) — I run routine commands directly and only confirm risky ones")
            self.bot.send_message(chat_id, f"Autonomy: {state}\nChange with: /auto on · off")
            return
        arg = parts[1].strip().lower()
        if arg in ("on", "yes", "true"):
            self.set_auto_mode(chat_id, True)
            self.bot.send_message(chat_id, "Autonomy ON. I'll run every command without asking, including destructive ones. /auto off to confirm risky commands again.")
        elif arg in ("off", "no", "false"):
            self.set_auto_mode(chat_id, False)
            self.bot.send_message(chat_id, "Autonomy OFF. I'll handle routine commands directly and only stop to confirm risky ones (delete, format, kill, etc.).")
        else:
            self.bot.send_message(chat_id, "Usage: /auto on · off")

    def handle_document(self, message):
        chat_id = message.chat.id
        self.ensure_initialised(chat_id)
        doc = message.document
        file_name = doc.file_name or "file"
        ext = Path(file_name).suffix.lower()

        # An image sent as a file (Telegram's uncompressed path) is an image: the same turn a photo gets, with the
        # original size handled like any image the agent views. Before this it was "File type '.jpg' not supported"
        # (2026-10-13 15:28).
        if ext in self.IMAGE_FILE_TYPES or (getattr(doc, "mime_type", "") or "").startswith("image/"):
            downloaded = self.fetch_file(doc)
            try:
                media_type, encoded, _ = self._image_for_model(downloaded)
            except Exception:
                self._run_note_turn(chat_id, message, f"[Alex sent an image file, {file_name}, but it could not be opened as an image.]")
                return
            self._last_photo[chat_id] = downloaded
            caption = f"[Alex sent an image file: {file_name}]" + (" " + message.caption if message.caption else "")
            reply_note = self._reply_context(message, with_image=False)[0]
            self._run_image_turn(chat_id, [(media_type, encoded)], reply_note + caption, message.date)
            return

        downloaded = self.fetch_file(doc)
        _DOC_LIMIT = 50_000
        try:
            if ext == ".docx":
                from docx import Document
                document = Document(io.BytesIO(downloaded))
                text = "\n".join(p.text for p in document.paragraphs if p.text.strip())
            elif ext == ".xlsx" and self.config.xlsx_uploads:
                from tabularium.tools.spreadsheet_upload import save_workbook
                text = save_workbook(self.AGENT_DIR, file_name, downloaded)
            elif ext == ".txt":
                text = downloaded.decode("utf-8", errors="replace")
            elif ext == ".pdf":
                import pypdf
                pdf = pypdf.PdfReader(io.BytesIO(downloaded))
                text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            else:
                text = None
                note = (f"[Alex sent a file, {file_name}. Files of type {ext or 'without an extension'} cannot be read "
                        f"here; images and {self.config.document_types} can.]")
        except Exception as e:
            text = None
            note = f"[Alex sent a file, {file_name}, but it could not be read ({type(e).__name__}: {e}).]"
        if text is not None:
            note = f"[Alex uploaded a file: {file_name}]\n\n{text[:_DOC_LIMIT]}"
            if len(text) > _DOC_LIMIT:
                note += f"\n\n[File truncated: only the first {_DOC_LIMIT:,} of {len(text):,} characters are included.]"
        self._run_note_turn(chat_id, message, note)
