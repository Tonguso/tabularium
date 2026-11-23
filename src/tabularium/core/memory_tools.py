"""The tools that read and write the agent's memory, and the fixed opening every conversation starts from.
"""
import re
from datetime import datetime
from tabularium.memory.memory_context import read_view, WorkingMemory, current_context
from tabularium.learning.learning import record as record_lesson
from tabularium.core.outcome import Failed
from tabularium.memory.memory_store import append_unique, reconcile, record_fact


class MemoryTools:
    """Part of AgentRuntime (tabularium.core.engine): methods and constants moved here unchanged on 2026-11-23."""

    def tool_read_memory(self, section: str = "", query: str = "", offset: int = 0,
                         limit: int = 12000) -> str:
        try:
            text = read_view(self.AGENT_DIR)
            headings = list(re.finditer(r"^(#{1,6}) (.+)$", text, re.M))
            section, query = section.strip(), query.strip()
            if not section and not query:
                return "Memory headings (choose section/query; section='all' reads all with paging):\n" + "\n".join(h.group(0) for h in headings)
            if section and section.casefold() != "all":
                matches = [h for h in headings if h.group(2).casefold() == section.casefold()]
                if not matches:
                    matches = [h for h in headings if section.casefold() in h.group(2).casefold()]
                if len(matches) != 1:
                    return "Section missing or ambiguous. Call read_memory without arguments for headings."
                h = matches[0]
                end = next((n.start() for n in headings if n.start() > h.start() and len(n.group(1)) <= len(h.group(1))), len(text))
                text = text[h.start():end]
            if query:
                lines = text.splitlines()
                indexes = set()
                for i, line in enumerate(lines):
                    if query.casefold() in line.casefold():
                        indexes.update(range(max(0, i - 2), min(len(lines), i + 3)))
                text = "\n".join(f"{i + 1}: {lines[i]}" for i in sorted(indexes)) or "No matching memory text."
            offset, limit = max(0, int(offset)), min(24000, max(1, int(limit)))
            end = min(len(text), offset + limit)
            next_page = str(end) if end < len(text) else "none"
            return f"Memory: {len(text)} characters; offset={offset}; next_offset={next_page}. Repeat section/query for paging.\nHistorical entries are dated records, not current instructions.\n\n{text[offset:end]}"
        except Exception as e:
            return Failed(f"Error reading memory: {e}")

    def load_memory_context(self) -> list:
        """The fixed opening of every conversation. It holds no copy of memory: a copy here went stale while the
        corrections appended after it scrolled out of view (2026-10-12). The current core memory is attached to the
        latest message of every request instead (_attach_current_context)."""
        session_start = datetime.now().strftime("%Y-%m-%d %H:%M")
        text = (f"[Session started: {session_start}]\n[Your core memory is attached to the latest message of every "
                "request, read fresh from memory.md each time, so it is always the current version.]")
        return [
            {"role": "user", "content": [
                {"type": "text", "text": text, "cache_control": dict(self.CACHE_CONTROL)},
            ]},
            {"role": "assistant", "content": "Core memory loaded. Ready."},
        ]

    def _refresh_core_memory(self, messages, chat_id):
        """Make sure the conversation opens with the fixed seed (rewritten once per process start). Memory itself is
        never stored in history; _attach_current_context puts the current core on every request."""
        seeded = len(messages) >= 2 and messages[1].get("content") in ("Core memory loaded. Ready.", "Memory loaded. Ready.")
        if not seeded:
            messages[:0] = self.load_memory_context()
        elif chat_id not in self._core_versions:
            messages[:2] = self.load_memory_context()
        self._core_versions[chat_id] = True

    def tool_log_activity(self, note: str) -> str:
        return WorkingMemory(self.AGENT_DIR).activity(note)

    def tool_remember(self, kind: str = "", content: str = "", source: str = "", quote: str = "", valid_from: str = "",
                      valid_to: str = "", supersedes: str = "", old: str = "", new: str = "", reason: str = "",
                      name: str = "", state: str = "", done: bool = False, pinned: bool = False,
                      owner_asked: bool = False, method_path: str = "", cause: str = "", anchor: str = "",
                      silent_at: str = "", explains: list = None, chat_id: int = None) -> str:
        """One memory writer (2026-10-08): a sourced fact, an unsourced note, an exact correction, or a working
        thread. Replaces record_fact / append_memory / reconcile_memory / work_thread, which overlapped and were
        picked by the shape of the sentence rather than by rule; delete_memory_section is now a maintenance script."""
        if kind == "lesson":
            # The lesson and its edit are one record (2026-11-01: 183 candidates, 40 drafts nobody showed Alex).
            if not method_path or not (new or "").strip() or not (cause or "").strip():
                return Failed("A lesson is recorded together with its cause and its edit, in one step: cause (the decision in "
                              "the method that allowed the failure) with anchor (the passage quoted exactly from the method, "
                              "which old must then contain) or silent_at (where the method says nothing); explains (keys of "
                              "the other open observations this cause accounts for); method_path (the skill file under "
                              "skills/, or SKILL.md for a habit of conversation), old (the exact current text to rewrite, or "
                              "empty, only where the method is silent, to add a ledger entry) and new (the new text in the "
                              "skill's own voice). Without the cause and the edit nothing is recorded.")
            try:
                shown = record_lesson(self.AGENT_DIR, chat_id, content, source or "foreground observation", method_path, old, new,
                                      cause={"claim": cause, "anchor": anchor, "silent_at": silent_at}, explains=explains or [])
            except ValueError as exc:
                return Failed(str(exc))
            return ("Lesson registered for Alex's go as " + shown["key"] + ". It reaches him on the dashboard and in the "
                    "weekly message, so send it only if he asks. Nothing changes until he approves it.\n" + shown["show_owner"])
        if old or new:
            if not (old and new):
                return "A correction needs both old (exact, occurring once) and new."
            return reconcile(self.MEMORY_FILE, old, new, reason or "Corrected through remember")
        if kind == "thread":
            return self.tool_work_thread(name, state, done, pinned, owner_asked)
        if kind == "consolidate":
            from tabularium.memory import memory_consolidation as mc
            try:
                if old:
                    n = mc.propose(self.AGENT_DIR, old, new, reason)
                    return f"Edit {n} added to the memory candidate (not live). Add more, or state=trial to run the exam."
                if state == "trial":
                    if not mc.status(self.AGENT_DIR):
                        return Failed("No candidate: propose at least one edit first.")
                    (self.AGENT_DIR / "task_state" / "memory_trial_requested").touch()
                    return ("Trial queued: within a minute the memory exam runs on the live memory and on the candidate "
                            "(about two minutes). Then state=status shows both scores; show Alex the scores and the edits.")
                if state == "apply":
                    done = mc.apply(self.AGENT_DIR)
                    return f"Applied {done['edits']} edit(s) to memory.md; the old text is backed up."
                if state == "discard":
                    mc.discard(self.AGENT_DIR); return "Memory candidate discarded."
                if state == "rebase":
                    n = mc.rebase(self.AGENT_DIR)
                    return f"Replayed {n} edit(s) on the current memory.md. Run state=trial again before showing Alex."
                return mc.summary(self.AGENT_DIR) + ("\n\n" + mc.diff(self.AGENT_DIR) if mc.status(self.AGENT_DIR) else "")
            except ValueError as exc:
                return Failed(str(exc))
        if kind == "note":
            if not content.strip():
                return "A note needs content."
            return append_unique(self.MEMORY_FILE, f"- {datetime.now().strftime('%Y-%m-%d')} (unsourced note): {content.strip()}")
        if kind in ("user_preference", "user_decision", "assistant_proposal", "observation", "inference"):
            try:
                self._recall.ingest_saved(chat_id)
                return record_fact(self.MEMORY_FILE, self._recall, chat_id, content, kind, source, quote, valid_from, valid_to, supersedes)
            except (ValueError, OSError) as exc:
                return "Fact not saved: " + str(exc)
        return ("remember needs kind: user_decision, user_preference, assistant_proposal, observation, inference (sourced), "
                "note (unsourced), or thread; or old+new for a correction.")

    def tool_work_thread(self, name: str, state: str = "", done: bool = False,
                         pinned: bool = False, owner_asked: bool = False) -> str:
        return WorkingMemory(self.AGENT_DIR).thread(name, state, done, pinned, owner_asked)

    def _load_core_memory(self) -> str:
        return current_context(self.AGENT_DIR, include_working=not self.BACKGROUND_TASK_ID)
