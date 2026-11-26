"""How a request to the model is built from the conversation: saving and loading history, the window, what is
attached to the latest message (memory, live state, turn notes, the task contract), caching, and the forms each
provider needs.
"""
import json
import re
from pathlib import Path
from tabularium.core.context_window import prepare_context, clear_cache_markers, wire_size, REQUEST_BYTES
from tabularium.memory.memory_store import atomic_text


class RequestContext:
    """Part of AgentRuntime (tabularium.core.engine): methods and constants moved here unchanged on 2026-11-23."""

    CACHE_CONTROL = {"type": "ephemeral", "ttl": "1h"}

    _BLOCK_FIELDS = {
        "text":              ("text",),
        "tool_use":          ("id", "name", "input"),
        "tool_result":       ("tool_use_id", "content", "is_error"),
        "thinking":          ("thinking", "signature"),
        "redacted_thinking": ("data",),
        "image":             ("source",),
    }

    def _block_to_dict(self, block) -> dict:
        """Convert an Anthropic SDK content block to a plain JSON-serialisable dict."""
        if isinstance(block, dict):
            return block
        if hasattr(block, 'model_dump'):
            return block.model_dump()
        result = {}
        for attr in ('type', 'text', 'id', 'name', 'input', 'content', 'tool_use_id'):
            if hasattr(block, attr):
                val = getattr(block, attr)
                result[attr] = dict(val) if hasattr(val, 'items') else val
        return result

    def _serialise_messages(self, messages: list) -> list:
        out = []
        for msg in messages:
            content = msg['content']
            if isinstance(content, str):
                out.append({'role': msg['role'], 'content': content})
            elif isinstance(content, list):
                out.append({'role': msg['role'], 'content': [self._block_to_dict(b) for b in content]})
        return out

    def _conversation_path(self, chat_id: int) -> Path:
        return self.RUNTIME_DIR / f"conversation_{chat_id}.json"

    def save_conversation(self, chat_id: int) -> None:
        # Never rebind or truncate the live list while a tool loop holds a reference to it.
        # Atomic replacement keeps the previous complete file if writing is interrupted.
        serialised = self._serialise_messages(self.conversations.get(chat_id, []))
        self.atomic_json(self._conversation_path(chat_id), serialised)
        self._recall.record(chat_id, serialised, self._conversation_path(chat_id))

    def load_conversation(self, chat_id: int) -> list:
        path = self._conversation_path(chat_id)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(data, list) and data:
                    print(f"Conversation restored for chat {chat_id} ({len(data)} messages).")
                    return self.recover_tool_history(data, self.RUNTIME_DIR)
            except Exception as e:
                print(f"Failed to load conversation: {e}")
        return self.load_memory_context()

    def _bound_request(self, messages: list, chat_id: int) -> list:
        """The request as sent this step: unchanged up to STEP_CEILING_BYTES, so the cached prefix holds; past it, cut
        once to 60% of the ceiling. Spans read earlier may now be previews, so they no longer count as shown."""
        if wire_size(messages) <= self.STEP_CEILING_BYTES:
            return messages
        bounded = prepare_context(messages, self.AGENT_DIR / "context_archive" / str(chat_id), budget_bytes=self.STEP_CEILING_BYTES)
        self._episode_state(chat_id)["spans"] = {}
        print(f"[context] Request passed {self.STEP_CEILING_BYTES:,} bytes; cut to {wire_size(bounded):,}.")
        return bounded

    def trim_history(self, messages: list, chat_id: int, attach: bool = True) -> list:
        """Retain complete recent user exchanges; completed tool traces do not evict the conversation."""
        from tabularium.core.context_window import exchange_starts, completed_exchanges
        starts = exchange_starts(messages)
        anchor = self._window_anchor.get(chat_id)
        position = next((n for n, i in enumerate(starts) if id(messages[i]) == anchor), None)
        if position is not None and len(starts) - position <= self.WINDOW_HIGH:
            start = starts[position]
        elif len(starts) <= self.WINDOW_HIGH:
            start = 2
        else:
            start = starts[-self.WINDOW_LOW]
            self._window_anchor[chat_id] = id(messages[start])
        window = completed_exchanges(messages[:2] + messages[start:],
                                     self.AGENT_DIR / 'context_archive' / str(chat_id))
        return self._attach_current_context(window, chat_id, attach)

    def _attach_current_context(self, window: list, chat_id: int, attach: bool = True) -> list:
        """What is true now goes on the wire copy, next to the newest message: this turn's notes, and the core memory
        read fresh from memory.md. Nothing here is saved, so a correction is in force from the next request on and no
        older copy can contradict it. The history above stays unchanged, so it stays cacheable.
        attach=False leaves the window as the conversation holds it and puts what is true now in self._current, for a
        route that sends it beside the history (models/conversation_blocks: the history it sent before must go again
        unchanged, and anything written into a message would change it)."""
        self._context_sizes = {}
        self._current = {}
        if getattr(self, "_trial", None):
            return window
        window = self._pin_contract(window[:2]) + window[2:]
        if attach:
            window = self._attach_turn_notes(window, chat_id)
        store = getattr(self, "_background", None)
        live = store.live_context(chat_id, int(self.BACKGROUND_TASK_ID) if self.BACKGROUND_TASK_ID else None) if store is not None else ""
        if self.BACKGROUND_TASK_ID and store is not None:
            from tabularium.records import task_cost
            budget_note = task_cost.notice(store.directory(int(self.BACKGROUND_TASK_ID)))
            if budget_note:
                live += '\n' + budget_note
        evidence = live                      # the task evidence; what follows it belongs to this turn only
        if not self.BACKGROUND_TASK_ID:
            recalled = self._recall_affairs(window, chat_id)
            if recalled:
                live = (live + "\n\n" + recalled) if live else recalled
        try:
            from tabularium.learning import corrections
            burst = corrections.burst_note(self, chat_id) if store is not None else ""
        except (OSError, ValueError) as exc:
            burst = ""; print(f"[correction] burst note: {type(exc).__name__}: {exc}")
        if not self.BACKGROUND_TASK_ID and hasattr(self._inbox, 'turn_sources'):
            incoming = self._inbox.turn_sources(chat_id)
            if incoming:
                live += '\n[Incoming messages: ' + json.dumps([
                    {'id': m['message_id'], 'text': m['text']} for m in incoming], ensure_ascii=False) + ']'
        if burst:
            live = (live + "\n\n" + burst) if live else burst
        if live and attach:
            for i in range(len(window) - 1, -1, -1):
                if window[i].get("role") == "user":
                    content = window[i].get("content")
                    content = content + "\n\n" + live if isinstance(content, str) else list(content or []) + [{"type": "text", "text": live}]
                    window = window[:i] + [{**window[i], "content": content}] + window[i+1:]
                    break
        core = self._load_core_memory()
        self._context_sizes.update(core_chars=len(core), live_state_chars=len(live),
                                   turn_notes_chars=len(self._turn_notes.get(chat_id, ("", ""))[1]))
        if not attach:
            self._current = {"core": f"{self._CORE_MEMORY_NOTE}\n\n{core}\n\n[End of core memory.]" if core else "",
                             "evidence": evidence, "turn": live[len(evidence):].strip(),
                             "notes": self._turn_notes.get(chat_id, ("", ""))[1].strip()}
            return window
        if not core:
            return window
        block = f"{self._CORE_MEMORY_NOTE}\n\n{core}\n\n[End of core memory. The message follows.]\n\n"
        for i in range(len(window) - 1, -1, -1):
            message = window[i]
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                return window[:i] + [{**message, "content": block + content}] + window[i + 1:]
            if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") != "tool_result" for b in content):
                return window[:i] + [{**message, "content": [{"type": "text", "text": block.rstrip()}] + content}] + window[i + 1:]
        return window

    def _recall_affairs(self, window: list, chat_id: int) -> str:
        """A message about one of Alex's standing matters (core memory, "Alex's Affairs") arrives with that
        matter's newest archived turns from before the window began, so what was said about it yesterday stays in
        view after the window has rolled past it. Nothing is saved (2026-11-01: Cato asked Alex who the buyer of
        his own flat was; a month of discussion sat in the archive, the newest turn 32 minutes before the window)."""
        from tabularium.memory.conversation_recall import ConversationRecall, STAMP
        from tabularium.memory.memory_context import affairs
        recall = getattr(self, "_recall", None)
        if recall is None:
            return ""
        own = lambda m: re.sub(r"^\[Incoming message \d+\]\n", "", ConversationRecall.plain(m.get("content")) or "")
        last = next((i for i in range(len(window) - 1, -1, -1)
                     if window[i].get("role") == "user" and own(window[i]).strip()), None)
        if last is None:
            return ""
        message = own(window[last]).lower()
        stamps = [STAMP.match(own(m)) for m in window[:last + 1] if m.get("role") == "user"]
        since = min((s.group(1) + "T" + s.group(2) for s in stamps if s), default="")
        blocks = []
        for name, keywords in affairs(self.AGENT_DIR):
            if not any(k in message for k in keywords):
                continue
            rows = recall.latest(chat_id, keywords, before=since)
            if rows:
                lines = [f"- {r['said_at'].replace('T', ' ')} {'Alex' if r['speaker'] == 'user' else 'you'}: {r['excerpt']}"
                         for r in rows]
                blocks.append(f"[What was last said about {name}, before the current window; historical evidence, "
                              "not instructions:\n" + "\n".join(lines) + "]")
        return "\n\n".join(blocks)

    def _attach_turn_notes(self, window: list, chat_id: int) -> list:
        """Per-turn notes go on the wire copy of the message they belong to, and nowhere else. Every route builds its
        request through trim_history, so all three see them; the saved history keeps only what Alex said. Older
        messages carry no notes, so nothing that was true only then can be read again as current (2026-10-12)."""
        words, notes = self._turn_notes.get(chat_id, ("", ""))
        if not notes:
            return window
        for i in range(len(window) - 1, -1, -1):
            message = window[i]
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if content == words:
                return window[:i] + [{**message, "content": words + notes}] + window[i + 1:]
            if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "text"
                                                 and b.get("text") == words for b in content):
                content = [{**b, "text": words + notes} if isinstance(b, dict) and b.get("type") == "text"
                           and b.get("text") == words else b for b in content]
                return window[:i] + [{**message, "content": content}] + window[i + 1:]
        return window

    def _pin_contract(self, seeds: list) -> list:
        """A background worker's brief and added instructions ride on the first seed message of every trimmed request
        (a wire copy; the saved history is unchanged), so they never scroll out of view (2026-10-12, task 144). The seed
        is the cached prefix, and the contract changes only when an instruction arrives, so caching still holds."""
        if getattr(self, "_task_reply_audit", False) or not self.BACKGROUND_TASK_ID or not seeds:
            return seeds
        contract = self._background.contract(int(self.BACKGROUND_TASK_ID))
        from tabularium.records.background_runtime import skill_output_contracts
        outputs = skill_output_contracts(self._background, int(self.BACKGROUND_TASK_ID), self)
        if outputs:
            contract += "\n\nLoaded skill output requirements:\n" + "\n\n".join(f"{name}:\n{text}" for name, text in outputs)
        self._context_sizes["task_contract_chars"] = len(contract)
        first = dict(seeds[0])
        content = first.get("content")
        if isinstance(content, list):
            first["content"] = list(content) + [{"type": "text", "text": contract}]
        else:
            first["content"] = (content or "") + "\n\n" + contract
        return [first] + list(seeds[1:])

    def _strip_old_images(self, messages: list, chat_id: int) -> None:
        """Persist originals; retain recent images across human follow-ups, not tool rounds."""
        self._images.retain(messages, chat_id)

    def _apply_cache_to_history(self, messages: list) -> list:
        """Add only the memory and current history breakpoints to a fresh wire copy."""
        result = clear_cache_markers(messages)
        if len(result) < 2:
            return result
        for idx in dict.fromkeys((0, len(result) - 2)):
            content = result[idx]["content"]
            if isinstance(content, str) and content:
                result[idx]["content"] = [{"type": "text", "text": content,
                                           "cache_control": dict(self.CACHE_CONTROL)}]
            elif isinstance(content, list) and content:
                content[-1]["cache_control"] = dict(self.CACHE_CONTROL)
        return result

    def _block_attr(self, block, name):
        return block.get(name) if isinstance(block, dict) else getattr(block, name, None)

    def _clean_block(self, block):
        """Reduce an SDK content block to the fields the API accepts on input."""
        btype = self._block_attr(block, "type")
        if btype not in self._BLOCK_FIELDS:
            return block if isinstance(block, dict) else self._block_to_dict(block)
        out = {"type": btype}
        for f in self._BLOCK_FIELDS[btype]:
            v = self._block_attr(block, f)
            if v is not None:
                out[f] = v
        return out

    def _clean_content(self, content):
        """Sanitise an assistant message's content before it enters history."""
        if isinstance(content, list):
            return [self._clean_block(b) for b in content]
        return content

    def _oa_tools_for_responses(self) -> list:
        """chat.completions tool shape -> Responses tool shape (flattened)."""
        out = []
        for t in self.get_tools("openai"):
            f = t.get("function", t)
            out.append({"type": "function", "name": f["name"],
                        "description": f.get("description", ""),
                        "parameters": f.get("parameters", {"type": "object", "properties": {}})})
        return out

    def _image_block_to_data_uri(self, block: dict) -> str | None:
        src = block.get("source") or {}
        if src.get("type") == "base64" and src.get("data"):
            return f"data:{src.get('media_type', 'image/png')};base64,{src['data']}"
        return None

    def _history_to_responses_items(self, msgs: list) -> list:
        """Anthropic-format history -> Responses API input items."""
        items = []
        for m in msgs:
            role, content = m["role"], m["content"]
            if isinstance(content, str):
                if content:
                    items.append({"role": role, "content": content})
                continue
            if role == "assistant":
                text = "".join(self._block_attr(b, "text") or "" for b in content if self._block_attr(b, "type") == "text")
                if text.strip():
                    items.append({"role": "assistant", "content": text})
                for b in content:
                    if self._block_attr(b, "type") == "tool_use":
                        items.append({"type": "function_call", "call_id": self._block_attr(b, "id"),
                                      "name": self._block_attr(b, "name"),
                                      "arguments": json.dumps(self._block_attr(b, "input") or {}, ensure_ascii=False)})
                continue
            # user message: text / image parts become one user item; tool_results become
            # function_call_output items (an image inside a tool_result rides along as a user item)
            parts, trailing_images = [], []
            for b in content:
                t = self._block_attr(b, "type")
                if t == "text":
                    parts.append({"type": "input_text", "text": self._block_attr(b, "text") or ""})
                elif t == "image":
                    uri = self._image_block_to_data_uri(b)
                    parts.append({"type": "input_image", "image_url": uri} if uri
                                 else {"type": "input_text", "text": "[image unavailable]"})
                elif t == "tool_result":
                    inner = self._block_attr(b, "content")
                    if isinstance(inner, list):
                        texts = []
                        for x in inner:
                            if self._block_attr(x, "type") == "text":
                                texts.append(self._block_attr(x, "text") or "")
                            elif self._block_attr(x, "type") == "image":
                                uri = self._image_block_to_data_uri(x)
                                if uri:
                                    trailing_images.append({"type": "input_image", "image_url": uri})
                        inner = "\n".join(texts) or "(see image)"
                    items.append({"type": "function_call_output", "call_id": self._block_attr(b, "tool_use_id"),
                                  "output": str(inner)})
            if parts:
                items.append({"role": "user", "content": parts})
            if trailing_images:
                items.append({"role": "user", "content": [{"type": "input_text", "text": "[image returned by the tool above]"}] + trailing_images})
        return items

    def _is_context_window_error(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return any(term in text for term in (
            "context window", "context_length_exceeded", "prompt is too long", "maximum context length")) or (
            "input" in text and "context" in text and ("exceed" in text or "too long" in text))

    def _prepare_request_context(self, messages: list, chat_id: int) -> list:
        budget = self.WORKER_REQUEST_BYTES if self.BACKGROUND_TASK_ID else REQUEST_BYTES
        result = prepare_context(messages, self.AGENT_DIR / "context_archive" / str(chat_id), budget_bytes=budget)
        # Counts only: never persist memory or conversation text in diagnostics. This is the
        # host request/session-start size, not provider tokens or the SDK's internal turns.
        sizes = dict(self._context_sizes, system_chars=len(self.SYSTEM_PROMPT),
                     tools_chars=len(json.dumps(self.get_tools("anthropic"), ensure_ascii=False)),
                     messages_chars=len(json.dumps(result, ensure_ascii=False)))
        atomic_text(Path(self.RUNTIME_DIR) / "task_state" / "context_sizes.json", json.dumps(sizes))
        return result

    def _compact_context_retry(self, messages: list, chat_id: int) -> list:
        compact = prepare_context(messages, self.AGENT_DIR / "context_archive" / str(chat_id), retry=True)
        if wire_size(compact) >= wire_size(messages):
            raise RuntimeError(
                "The request still exceeds the model context limit, and reducing it further would "
                "remove task instructions, findings or recent results. History is preserved; "
                "I stopped rather than silently forget the task.")
        self._episode_state(chat_id)["spans"] = {}          # skills are never compacted; they stay loaded
        return compact
