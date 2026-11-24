"""The agent's scheduled checks of its own memory: the review, the trial, the exam and the replay of the owner's
corrections.
"""
import json
import time


class MemoryChecks:
    """Part of AgentApp (tabularium.core.agent_app): methods moved here unchanged on 2026-11-24."""

    def run_memory_review(self):
        """Report clean-up candidates in memory.md; the agent proposes them to Alex on his next turn."""
        from tabularium.channels.owner_telegram import OWNER_CHAT_ID
        from tabularium.memory import memory_review, memory_exam
        examiner = ((lambda prompt, schema: memory_exam.examiner_call(self, prompt, schema))
                    if getattr(self, "anthropic_client", None) is not None else None)
        result = memory_review.run(self.AGENT_DIR, chat=OWNER_CHAT_ID, recall=self._recall, examiner=examiner)
        print(f"[memory_review] {result['candidates']} candidate(s); core {result['core_lines']} of {result['total_lines']} lines; "
              f"{len(result.get('changed_facts', []))} fact(s) his later messages changed" + (f"; stopped early: {result['error']}" if result.get('error') else ""))
        return result

    def memory_review_note(self) -> str:
        """The one-time note that hands a fresh review to the agent (empty once shown or when there is nothing)."""
        path = self.AGENT_DIR / "task_state" / "memory_review.json"
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        if result.get("shown_at") or not result.get("candidates"):
            return ""
        result["shown_at"] = time.time()
        self.atomic_json(path, result)
        return (f"\n\n[Weekly memory review: {result['candidates']} candidate clean-up(s) in {path.with_suffix('.md')} "
                f"({len(result['duplicates'])} near-duplicate bullets, {len(result['expired_facts'])} expired facts, "
                f"{len(result['stale_active'])} ACTIVE sections without a recent date, {len(result.get('changed_facts', []))} "
                f"sourced facts his later messages changed). When convenient, read it and propose "
                "the edits to Alex in one short message; change nothing until he agrees. Clean-ups go through "
                "remember(kind=consolidate): gather the edits, state=trial, show him the exam before/after, apply on his go.]")

    def run_memory_trial(self):
        """The memory exam on the live memory and on the consolidation candidate, same questions (2026-10-20)."""
        from tabularium.channels.owner_telegram import OWNER_CHAT_ID
        from tabularium.memory import memory_consolidation
        self.ensure_initialised(OWNER_CHAT_ID)
        with self.get_chat_lock(OWNER_CHAT_ID):
            t = memory_consolidation.trial(self, OWNER_CHAT_ID)
        print(f"[memory_trial] live {t['before']} -> candidate {t['after']}")
        return t

    def run_correction_eval(self):
        """Once a day, when the owner has been quiet for a while, replay his recorded corrections against the agent
        as it is now and record how many it repeats (tabularium.learning.correction_eval). `tabularium corrections`
        asks for a run now. Returns the record, or None when no run was due."""
        from tabularium.channels.owner_telegram import OWNER_CHAT_ID
        from tabularium.learning import correction_eval
        if not self.config.role.learns:
            return None
        flag = self.AGENT_DIR / "task_state" / "correction_eval_requested"
        last = correction_eval.latest(self.AGENT_DIR)
        due = flag.exists() or last is None or time.time() - last["time"] >= self.CORRECTION_EVAL_SECONDS
        if not due or self._inbox.idle_seconds() < self.CORRECTION_EVAL_QUIET:
            return None
        flag.unlink(missing_ok=True)
        self.ensure_initialised(OWNER_CHAT_ID)
        record = correction_eval.run(self, OWNER_CHAT_ID, quiet=lambda: self._inbox.idle_seconds() > 0)
        correction_eval.save(self.AGENT_DIR, record)
        print(f"[correction_eval] {correction_eval.summary(record)} ({record['seconds']}s)")
        return record

    def run_memory_exam(self):
        """Ask the agent a few questions about what Alex told him this week; record the result, send nothing."""
        from tabularium.channels.owner_telegram import OWNER_CHAT_ID
        from tabularium.memory import memory_exam
        self.ensure_initialised(OWNER_CHAT_ID)
        with self.get_chat_lock(OWNER_CHAT_ID):
            record = memory_exam.run_exam(self, OWNER_CHAT_ID)
        memory_exam.save(self.AGENT_DIR, record)
        print(f"[memory_exam] {'passed' if record['ok'] else 'FAILED'}: {memory_exam.summary(record)} ({record['seconds']}s)")
        return record
