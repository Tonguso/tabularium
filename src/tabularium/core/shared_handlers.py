"""Behaviour shared by all four agents, as methods on the runtime (2026-10-11, finishing the AgentRuntime refactor).

These 21 functions were identical in cato/tacitus/callimachus/vitruvius telegram.py apart from the agent's name; each
agent file now keeps only its tools, prompt and configuration. The handlers are registered with the bot in each
telegram.py (one line each) because the bot and inbox objects are created there. Any name they need from the agent
(AGENT_DIR, MEMORY_FILE, OPENAI_REASONING_EFFORT, bot, _inbox, ...) is an attribute AgentApp sets in its constructor."""
from tabularium.core import reply_envelope
from tabularium.core.outcome import Failed
from tabularium.core.wire_budget import IMAGE_ENCODED_CAP, encoded_size
import base64
import io
import json
import re
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from tabularium.memory.memory_context import read_view


TELEGRAM_FILE_CAP = 20 * 1024 * 1024       # the Bot API refuses getFile above this; nothing on our side can raise it


class FileTooBig(Exception):
    """An incoming file Telegram will not let a bot download. str(exc) is the sentence for Alex.

    2026-10-26: a 20.4 MB screenshot sent "as file" failed twice as "Task #2163 stopped with an error", and Cato
    guessed at the cause. The size is known before the download, so the gate is one place for every incoming file
    and the notice names the size and the limit; videos keep their own larger handling."""


class SharedHandlers:
    def fetch_file(self, ref, what="That file"):
        """Download one incoming Telegram file (photo size, document, voice) or raise FileTooBig with the sentence."""
        if getattr(ref, 'channel', None) == 'dashboard':
            from tabularium.channels.messaging import attachment_path
            return attachment_path(self.AGENT_DIR, ref.attachment).read_bytes()
        size = getattr(ref, "file_size", None) or 0
        name = getattr(ref, "file_name", None) or what
        notice = (f"{name} is {size / 1048576:.1f} MB; Telegram lets a bot download at most 20 MB, so I cannot read it. "
                  "Send it as a photo (Telegram compresses it) or a smaller file.")
        if size > TELEGRAM_FILE_CAP:
            raise FileTooBig(notice)
        try:
            return self.bot.download_file(self.bot.get_file(ref.file_id).file_path)
        except Exception as exc:
            if "file is too big" in str(exc).lower():
                raise FileTooBig(notice) from exc
            raise

    def _kill_headless_outlook(self) -> str:
        """Kill Outlook instances that have no visible window -- the ones COM starts on demand.
        A user-opened Outlook has a main window and is left alone. On 2026-09-30 a headless
        instance stopped answering COM and held send_email, and with it Cato, for an hour."""
        try:
            r = subprocess.run(
                ["powershell", "-NonInteractive", "-Command",
                 "Get-Process outlook -ErrorAction SilentlyContinue | "
                 "Where-Object { -not $_.MainWindowTitle } | "
                 "ForEach-Object { Stop-Process -Id $_.Id -Force; $_.Id }"],
                capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            pids = r.stdout.decode("utf-8", errors="replace").split()
            return f"killed headless Outlook pid(s) {', '.join(pids)}" if pids else "no headless Outlook found"
        except Exception as e:
            return f"could not kill Outlook: {e}"

    def _language_hint(self, text: str) -> str:
        """A per-turn factual note about which language Alex just used.

        Without this, Cato pattern-matches his OWN recent replies — if the last 30 messages are
        mostly his Chinese, he keeps writing Chinese even when Alex switches to English. In-context
        examples outweigh system-prompt rules, so the correction has to arrive with the new message.
        """
        pass  # state belongs to the runtime
        t = (text or "").strip()
        hint = ""
        if len(t) >= 3:
            frac = len(self._CJK.findall(t)) / max(len(t), 1)
            if frac == 0:
                hint = "\n\n[Alex wrote this in English. Reply in English, regardless of what language your previous replies were in.]"
            elif frac > 0.15:
                hint = "\n\n[Alex wrote this in Chinese. Reply in 简体中文 (never 繁體).]"
        # A bare "?" or "ok" carries no language. Without a hint the reply is anchored by nothing
        # -- on 2026-09-20 a lone "？" after a truncated turn came back in RUSSIAN. Carry the last
        # known language forward instead of sending nothing.
        if hint:
            self._last_language_hint = hint
        return hint or self._last_language_hint

    def _responses_call(self, client, *, model, instructions, input_items, tools, plan_mode):
        self._inbox.record_event("attempt", model, "subscription" if plan_mode else "api", "openai")
        if not plan_mode:
            return client.responses.create(
                model=model, instructions=instructions, input=input_items, tools=tools,
                reasoning={"effort": self.OPENAI_REASONING_EFFORT}, max_output_tokens=16000, store=False, prompt_cache_key=self._cache_key(),
                text=reply_envelope.OPENAI_TEXT, include=["reasoning.encrypted_content"]), "", [], []

        # ChatGPT plan-sharing requires streaming and forbids max_output_tokens.
        functions = [tool for tool in tools if tool.get("type") == "function"]
        namespaced = [tool for tool in tools if tool.get("type") != "function"]
        if functions:
            namespaced.append({"type": "namespace", "name": self.config.name, "description": f"{self.config.display}'s local tools",
                               "tools": functions})
        try:
            with client.responses.create(
                    model=model, instructions=instructions, input=input_items, tools=namespaced,
                    reasoning={"effort": self.OPENAI_REASONING_EFFORT}, store=False, stream=True, prompt_cache_key=self._cache_key(),
                    text=reply_envelope.OPENAI_TEXT, include=["reasoning.encrypted_content"]) as stream:
                completed = None
                output_text = []
                output_items = []
                event_types = []
                for event in stream:
                    event_types.append(event.type)
                    if event.type == "response.output_text.delta":
                        output_text.append(event.delta)
                    elif event.type == "response.output_item.done":
                        output_items.append(event.item)
                    if event.type == "error":
                        if getattr(event, "code", "") == "subscription_sharing_usage_limit_exceeded":
                            raise self._ChatGPTQuotaReached
                        raise RuntimeError(f"ChatGPT stream error ({getattr(event, 'code', 'unknown')}).")
                    if event.type == "response.failed":
                        err = getattr(event.response, "error", None)
                        code = getattr(err, "code", "") if err else ""
                        if code == "subscription_sharing_usage_limit_exceeded":
                            raise self._ChatGPTQuotaReached
                        raise RuntimeError(f"ChatGPT plan request failed ({code or 'unknown error'}).")
                    if event.type == "response.completed":
                        completed = event.response
                if completed is None:
                    # No completed response means nothing was executed, so handing this turn to the
                    # next route is safe. A plain RuntimeError here killed a foreground turn and a
                    # background worker on 2026-10-07 08:57 with the Claude route idle.
                    raise self._routing.Unavailable("ChatGPT plan stream ended without a completed response "
                                               f"(events: {','.join(event_types[-6:]) or 'none'})", 60)
                return completed, "".join(output_text), output_items, event_types
        except self._ChatGPTQuotaReached:
            raise
        except Exception as exc:
            body = getattr(exc, "body", None)
            err = (body.get("error") or body) if isinstance(body, dict) else {}
            err = err if isinstance(err, dict) else {}
            if err.get("code") == "subscription_sharing_usage_limit_exceeded":
                raise self._ChatGPTQuotaReached from exc
            if isinstance(exc, self._routing.Unavailable):
                raise
            # Nothing is executed until a completed response returns, so any failure without an HTTP
            # status is safe to hand to the next route: the SDK raises mid-stream errors such as
            # "Our servers are currently overloaded" as a bare APIError, which provider_routing would
            # otherwise treat as "needs attention" and kill the turn (task 80, 2026-10-07 14:13).
            # Status-coded errors (401 refresh, 429/5xx retry) and context-window errors keep their
            # own handling at the call site.
            if getattr(exc, "status_code", None) is None and not self._is_context_window_error(exc):
                raise self._routing.Unavailable("ChatGPT plan request failed before completion: "
                                           f"{type(exc).__name__}: {str(exc)[:160]}", 120) from exc
            raise

    def _sanitise_tool_history(self, messages: list) -> list:
        """REPAIR orphaned tool_use blocks so the API accepts the history.

        A turn interrupted mid tool-loop (timeout, /stop, crash) leaves an assistant message
        whose tool_use has no matching tool_result, which the API rejects with a 400.

        This used to TRUNCATE at the orphan — which silently discarded every LATER message,
        so Cato went permanently blind to everything Alex said afterwards. Instead we now
        insert a synthetic tool_result for the missing IDs, preserving the whole conversation.
        """
        # Pass 1 — drop tool_result blocks whose tool_use isn't in the immediately preceding
        # message. These appear when a trim or edit cuts an assistant/tool pair apart; the API
        # rejects the request with "unexpected tool_use_id found in tool_result blocks".
        cleaned = []
        for i, msg in enumerate(messages):
            if msg.get("role") == "user" and isinstance(msg.get("content"), list):
                prev = messages[i - 1] if i > 0 else None
                valid = set()
                if prev is not None and prev.get("role") == "assistant" and isinstance(prev.get("content"), list):
                    valid = {self._block_attr(b, "id") for b in prev["content"]
                             if self._block_attr(b, "type") == "tool_use"}
                kept = [b for b in msg["content"]
                        if self._block_attr(b, "type") != "tool_result"
                        or self._block_attr(b, "tool_use_id") in valid]
                if len(kept) != len(msg["content"]):
                    print(f"[sanitise] Dropped {len(msg['content']) - len(kept)} orphaned tool_result(s) at index {i}.")
                    if not kept:
                        continue                     # message was nothing but orphaned results
                    msg = {**msg, "content": kept}
            cleaned.append(msg)
        messages = cleaned

        # Pass 2 — the reverse case: a tool_use with no matching result gets a stub.
        out = []
        for i, msg in enumerate(messages):
            out.append(msg)
            if msg.get('role') != 'assistant':
                continue
            content = msg.get('content', '')
            if not isinstance(content, list):
                continue
            ids = [self._block_attr(b, 'id') for b in content
                   if self._block_attr(b, 'type') == 'tool_use' and self._block_attr(b, 'id')]
            if not ids:
                continue

            nxt = messages[i + 1] if i + 1 < len(messages) else None
            result_ids = set()
            if nxt is not None and nxt.get('role') == 'user' and isinstance(nxt.get('content'), list):
                result_ids = {self._block_attr(b, 'tool_use_id') for b in nxt['content']
                              if self._block_attr(b, 'type') == 'tool_result'}
            missing = [t for t in ids if t not in result_ids]
            if not missing:
                continue

            stub = [{"type": "tool_result", "tool_use_id": t,
                     "content": "[interrupted — no result recorded]"} for t in missing]
            if result_ids:
                # Next message already holds some results: merge the missing ones into it.
                merged = dict(nxt)
                merged["content"] = list(nxt["content"]) + stub
                messages[i + 1] = merged
            else:
                out.append({"role": "user", "content": stub})
            print(f"[sanitise] Repaired {len(missing)} orphaned tool_use block(s) at index {i}.")
        return out

    def _ts(self, sent=None) -> str:
        """Local datetime tag injected into every user message. Includes the weekday -- models
        can't reliably derive day-of-week from a date.

        `sent` is the Telegram message (or its unix `date`). When Alex sent it well before Cato
        got it -- the PC was asleep, offline or restarting, so Telegram held it -- the tag says so.
        Without this Cato sees only his own receive time, and once told Alex a three-hour wait
        was "three minutes", disputing something he had no way to see.
        """
        now = datetime.now()
        stamp = now.strftime("[%Y-%m-%d %A %H:%M]")
        unix = getattr(sent, "date", sent)
        if not isinstance(unix, (int, float)):
            return stamp
        sent_at = datetime.fromtimestamp(unix)
        late = (now - sent_at).total_seconds()
        if late < self.LATE_MESSAGE_SECONDS:
            return stamp
        mins = int(late // 60)
        wait = f"{mins // 60}h {mins % 60}m" if mins >= 60 else f"{mins}m"
        when = sent_at.strftime("%H:%M" if sent_at.date() == now.date() else "%Y-%m-%d %A %H:%M")
        return (f"{stamp} [Alex SENT this at {when} — you are only receiving it now, {wait} "
                f"later. You weren't receiving messages in between (computer asleep, offline or "
                f"restarting). If he mentions the wait, acknowledge it; don't dispute it. For "
                f"anything date-sensitive, like a daily log, use the time he sent it.]")

    def deliver_voice_reply(self, chat_id: int, reply: str) -> None:
        """Deliver a reply to a voice-originated turn: spoken or text, per the reply's declared modality and /voice mode."""
        if self._inbox.destination(chat_id) == 'dashboard':
            self.send_message(chat_id, reply)
            return
        mode = self.get_voice_mode(chat_id)
        modality, clean = getattr(reply, "modality", "text"), reply
        if mode == "off":
            want_voice = False
        elif mode == "on":
            want_voice = True
        else:  # auto — Cato decides per content
            want_voice = (modality == "voice")
        if want_voice and self._speak(chat_id, clean):
            return
        self.send_message(chat_id, clean)

    def handle_background_command(self, message):
        try:
            bits = (message.text or "").split(maxsplit=2)
            command = bits[0].split("@")[0][1:]
            if command == "tasks":
                rows = self._background.status(message.chat.id)
                text = "\n".join(f"{self._background.label(r)} — {r['status']} ({r.get('progress', {}).get('phase', 'waiting')})" for r in rows) or "No background tasks."
            elif command == "background":
                request = (message.text or "").split(maxsplit=1)
                if len(request) < 2:
                    raise ValueError("Usage: /background <task>")
                result = json.loads(self.tool_background_task("start", message.chat.id, request=request[1]))
                text = f"{self._background.label(result)} — {result['status']}"
            else:
                rest = (message.text or "").split(maxsplit=1)
                if len(rest) < 2:
                    raise ValueError(f'Usage: /{command} "task description" [instruction]')
                reference = re.fullmatch(r'"([^"]+)"(?:\s+(.*))?', rest[1])
                if reference:
                    name, instruction = reference.group(1), reference.group(2) or ""
                elif command in ("cancel", "resume"):
                    name, instruction = rest[1].strip(), ""
                else:
                    raise ValueError('Usage: /steer "task description" instruction')
                ident = self._background.resolve(message.chat.id, name)
                result = json.loads(self.tool_background_task(command, message.chat.id, ident, instruction))
                text = f"{self._background.label(result)} — {result['status']}"
                if command == "steer":
                    text += "\nInstruction queued."
            self.send_message(message.chat.id, text)
        except (ValueError, OSError) as exc:
            self.send_message(message.chat.id, str(exc))

    def handle_clear(self, message):
        chat_id = message.chat.id
        self.conversations[chat_id] = []
        self._conversation_path(chat_id).unlink(missing_ok=True)
        self.bot.send_message(chat_id, "Conversation cleared. Memory intact.")

    def handle_memory(self, message):
        chat_id = message.chat.id
        try:
            content = read_view(self.AGENT_DIR).strip()
            if not content:
                self.bot.send_message(chat_id, "Memory file is empty.")
            else:
                self.send_message(chat_id, content)
        except Exception as e:
            self.bot.send_message(chat_id, f"Error reading memory: {e}")

    def handle_model(self, message):
        status = self._routing.read(self.RUNTIME_DIR/'task_state'/'routing_state.json')
        rows = self._routing.config(self.AGENT_DIR)['routes']
        text = 'Model sequence: ' + ' → '.join(self._routing.label(r) for r in rows)
        if status.get('route'):
            label = 'Active: ' if status.get('status') == 'attempting' else 'Last used: ' if status.get('status') == 'completed' else 'Last attempted: '
            text += '\n' + label + self._routing.label(status['route']) + ' (' + status.get('status','unknown') + ')'
        else:
            text += '\nNo request has run through this sequence yet.'
        self.bot.send_message(message.chat.id, text + '\nChange models in the dashboard; /model is read-only.')

    def handle_openai_connection(self, message):
        chat_id = message.chat.id
        parts = message.text.strip().split(maxsplit=1)
        command = parts[0].split("@", 1)[0].lower()
        action = "connect" if command == "/openai_connect" else (parts[1].strip().lower() if len(parts) > 1 else "status")
        if action == "status":
            self.bot.send_message(chat_id, self._chatgpt_plan.status())
        elif action == "connect":
            self.bot.send_message(chat_id, "Opening ChatGPT sign-in on the computer running Cato. Finish the browser steps there.")

            def finish_sign_in():
                try:
                    email, enabled = self._chatgpt_plan.connect()
                    if enabled:
                        text = (f"Connected ChatGPT account {email}. Cato will use your ChatGPT plan first, "
                                "then fall back to GPT-6.1 API only when OpenAI reports the plan usage limit is reached.")
                    else:
                        text = (f"Signed in as {email}, but ChatGPT plan usage was not granted. "
                                "Cato will keep using the GPT-6.1 API. Free accounts are not eligible for plan usage; "
                                "Plus or Pro is required.")
                    self.bot.send_message(chat_id, text)
                except Exception as e:
                    print(f"ChatGPT sign-in failed: {e}")
                    self.bot.send_message(chat_id, f"ChatGPT sign-in failed: {e}")

            threading.Thread(target=finish_sign_in, daemon=True, name="chatgpt-sign-in").start()
        elif action == "disconnect":
            try:
                self._chatgpt_plan.disconnect()
                self.bot.send_message(chat_id, "ChatGPT connection removed. Cato will use the GPT-6.1 API.")
            except Exception as e:
                self.bot.send_message(chat_id, f"Could not remove the ChatGPT connection: {e}")
        elif action == "resume":
            if self._chatgpt_plan.resume_plan():
                self.bot.send_message(chat_id, "ChatGPT plan retries are enabled. Cato will try the plan first on his next turn.")
            else:
                self.bot.send_message(chat_id, "No ChatGPT plan-usage connection is enabled. Use /openai connect.")
        else:
            self.bot.send_message(chat_id, "Use /openai status, /openai connect, or /openai disconnect.")

    def handle_pending(self, message):
        self.send_message(message.chat.id, self._inbox.status(message.chat.id))

    def handle_dashboard_media(self, message):
        from types import SimpleNamespace
        from tabularium.channels.messaging import attachment_path, attachment_kind
        ref = message.attachment
        path = attachment_path(self.AGENT_DIR, ref)
        kind = attachment_kind(ref['name'])
        local = SimpleNamespace(channel='dashboard', attachment=ref, file_name=ref['name'],
                                file_size=path.stat().st_size, mime_type='')
        if kind == 'voice':
            message.voice = local
            self.handle_voice(message)
        elif kind == 'video':
            from tabularium.tools import video_intake
            self.ensure_initialised(message.chat.id)
            self.send_message(message.chat.id, 'Processing video…')
            try:
                result = video_intake.process_video(path)
                picks = [result['sheet']] + [p for _, p in video_intake.pick_attachments(result['frames'])]
                images = [self._image_for_model(Path(p).read_bytes())[:2] for p in picks]
                self._run_image_turn(message.chat.id, images, video_intake.describe(result, message.caption or ''), message.date)
            except video_intake.VideoError:
                self.send_message(message.chat.id, 'I could not process that video. It is saved; try a shorter MP4 clip.')
        else:
            message.document = local
            self.handle_document(message)

    def handle_photo(self, batch):
        chat_id = batch[0].chat.id
        self.ensure_initialised(chat_id)
        images = []
        for message in batch:
            downloaded = self.fetch_file(message.photo[-1], "That photo")
            self._last_photo[chat_id] = downloaded
            self._images.store(chat_id, downloaded, self.detect_media_type(downloaded),
                          message.caption or "", message.date, kind="photo")
            media_type, encoded, _ = self._image_for_model(downloaded)
            images.append((media_type, encoded))
        caption = '\n'.join(m.caption for m in batch if m.caption) or None
        reply_note = self._reply_context(batch[0], with_image=False)[0]
        if reply_note:
            caption = reply_note + (caption or "")
        # Preserve every accepted photo. Larger albums are handled in explicit batches.
        for start in range(0, len(images), self.MAX_IMAGES_PER_TURN):
            self._run_image_turn(chat_id, images[start:start+self.MAX_IMAGES_PER_TURN], caption, min(m.date for m in batch))

    def handle_reload(self, message):
        chat_id = message.chat.id
        seed = [
            {"role": "user",      "content": f"[Core memory reloaded mid-session]\n{self._CORE_MEMORY_NOTE}\n\n{self._load_core_memory()}"},
            {"role": "assistant", "content": "Core memory reloaded."},
        ]
        if chat_id in self.conversations:
            self.conversations[chat_id] = seed + self.conversations[chat_id][2:]  # replace old seed, keep history
        else:
            self.conversations[chat_id] = seed
        self.load_model_prefs(chat_id)          # pick up a hand-edited model file too
        cur = self.get_openai_model(chat_id) if self.get_provider(chat_id) == "openai" else self.get_anthropic_model(chat_id)
        self.bot.send_message(chat_id, f"Memory reloaded into current session. Model: {cur}")

    def handle_skills_cmd(self, message):
        """/skills — list installed skills. /skills <name> — tell Cato to use one now."""
        chat_id = message.chat.id
        parts = message.text.strip().split(maxsplit=1)
        sk = self.discover_skills()
        if not sk:
            self.bot.send_message(chat_id, f"No skills installed. Add one at {self.SKILLS_DIR}\\<name>\\SKILL.md")
            return
        if len(parts) == 1:
            lines = ["Installed skills:"] + [f"• {n} — {s['description']}" for n, s in sk.items()]
            lines.append("\nUse one now with: /skills <name>")
            self.bot.send_message(chat_id, "\n".join(lines))
            return
        want = parts[1].strip()
        if want not in sk:
            self.bot.send_message(chat_id, f"No skill '{want}'. Installed: {', '.join(sk)}")
            return
        # Inject as a normal user turn so Cato loads and applies it.
        self.ensure_initialised(chat_id)
        lock = self.get_chat_lock(chat_id)
        lock.acquire()
        try:
            messages = self.conversations[chat_id]
            messages.append({"role": "user", "content":
                             f"{self._ts()} [Alex asked you to use the '{want}' skill. Call the skill tool "
                             f"to load it, then follow it.]"})
            self.bot.send_chat_action(chat_id, "typing")
            reply = self._run_turn_with_guard(messages, chat_id)
            if reply is None:
                return
            self.save_conversation(chat_id)
            self.send_message(chat_id, reply)
        finally:
            lock.release()

    def handle_stop_cmd(self, message):
        for task in self._background.status(message.chat.id):
            if task["status"] in ("queued", "starting", "running"):
                self._background.control(message.chat.id, task["id"], "cancel")

        chat_id = message.chat.id
        cancelled = self._inbox.cancel(chat_id)
        busy = self._inbox.active_chat == chat_id or self.get_chat_lock(chat_id).locked()
        if busy:
            self._abort_requested[chat_id] = True
        self.send_message(chat_id, f"{'Stopping at the next model/tool boundary.' if busy else 'No task running.'} Cancelled {cancelled} queued messages.")

    def handle_usage(self, message):
        self.send_message(message.chat.id, self._inbox.usage_summary(message.chat.id))

    def handle_voice(self, message):
        chat_id = message.chat.id
        self.ensure_initialised(chat_id)

        if not self.OPENAI_API_KEY:
            self.send_message(chat_id, "Voice transcription requires OPENAI_API_KEY in .env.")
            return

        lock = self.get_chat_lock(chat_id)
        lock.acquire()

        try:
            from openai import OpenAI as OAClient
            downloaded = self.fetch_file(message.voice, "That voice message")

            audio = io.BytesIO(downloaded)
            audio.name = getattr(message.voice, "file_name", None) or "voice.ogg"
            if audio.name.endswith(".weba"):
                audio.name = str(Path(audio.name).with_suffix(".webm"))
            transcript = OAClient(api_key=self.OPENAI_API_KEY).audio.transcriptions.create(
                model=self.STT_MODEL, file=audio
            )
            text = self._clean_transcript(transcript.text)
            if not text:
                self.send_message(chat_id, "Couldn't transcribe the voice message.")
                return

            self._inbox.local.turn_messages = [(message.message_id, text)]
            # Keep transcription internal; Alex does not want a text echo.

            messages = self.conversations[chat_id]
            reply_note, reply_image = self._reply_context(message)
            caption = getattr(message, 'caption', None) or ''
            voice_text = f"{self._ts(message)} {reply_note}{text}{self.VOICE_REPLY_HINT}"
            if caption:
                voice_text += f"\nAlex's note: {caption}"
            messages.append({"role": "user", "content": [reply_image, {"type": "text", "text": voice_text}]
                             if reply_image else voice_text})
            if self._inbox.destination(chat_id) != 'dashboard':
                self.bot.send_chat_action(chat_id, "typing")
            reply = self._run_turn_with_guard(messages, chat_id)
            if reply is None:
                return
            if self._inbox.destination(chat_id) == 'dashboard':
                self.save_conversation(chat_id)
            self.deliver_voice_reply(chat_id, reply)
        except Exception as e:
            self.send_message(chat_id, f"Voice error: {e}")
        finally:
            lock.release()

    def _image_for_model(self, data: bytes):
        """Encode to the shared wire cap; never upscale an image to make its file smaller."""
        from PIL import Image
        original = Image.open(io.BytesIO(data))
        image = None
        try:
            dimensions = original.size
            if max(dimensions) <= self.VIEW_IMAGE_MAX_EDGE and encoded_size(len(data)) <= IMAGE_ENCODED_CAP:
                return self.detect_media_type(data), base64.standard_b64encode(data).decode(), dimensions
            image = original.convert("RGB")
            edge = min(max(dimensions), self.VIEW_IMAGE_MAX_EDGE)
            while edge >= 1:
                image.thumbnail((edge, edge), Image.Resampling.LANCZOS)
                for quality in (85, 70, 55, 40):
                    buffer = io.BytesIO()
                    try:
                        image.save(buffer, format="JPEG", quality=quality)
                        payload = buffer.getvalue()
                    finally:
                        buffer.close()
                    if encoded_size(len(payload)) <= IMAGE_ENCODED_CAP:
                        return "image/jpeg", base64.standard_b64encode(payload).decode(), dimensions
                if edge == 1:
                    break
                edge = max(1, int(edge * 0.75))
            raise ValueError(f"Image cannot fit the {IMAGE_ENCODED_CAP}-byte base64 wire budget, even at 1 pixel")
        finally:
            original.close()
            if image is not None:
                image.close()

    def tool_view_image(self, path: str):
        """Load a local image so Cato can actually SEE it.

        Returns tool_result content blocks (image + text) rather than a string. The API accepts
        a block list here, so it flows through the loop unchanged. Without this Cato is blind to
        anything he renders himself -- he can build a deck or a chart but never look at it.
        """
        p = Path(path)
        if not p.is_file():
            return f"No file at {path}"
        try:
            data = p.read_bytes()
            media_type = self.detect_media_type(data)
            if media_type not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
                return f"{p.name} is {media_type}, which isn't a viewable image format."
            media_type, encoded, (w, h) = self._image_for_model(data)
            return [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": encoded}},
                {"type": "text", "text": f"[{p.name} — {w}x{h}px. Look at it and say what is "
                                         f"actually wrong, not what you intended to build.]"},
            ]
        except Exception as e:
            return Failed(f"Could not read image {path}: {e}")
