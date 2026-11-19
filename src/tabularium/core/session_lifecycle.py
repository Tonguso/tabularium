"""Session boundaries are not task completion. Continue only from durable, settled evidence."""
import json
from tabularium.core.agent_runtime import atomic_json
from tabularium.core.outcome import AlreadyShown, Failed, Refused


class TaskWaiting(Exception):
    def __init__(self, decision):
        super().__init__('Task is awaiting a recorded decision.')
        self.decision = decision


class SessionLimit(Exception):
    pass


def completed_actions(messages):
    return {b.get("tool_use_id") for m in messages if isinstance(m.get("content"), list)
            for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"
            and not str(b.get("content", "")).startswith("Outcome unknown:")}


def continue_turn(agent, messages, chat, run):
    sessions = 0
    while True:
        before = completed_actions(messages)
        try:
            return run()
        except TaskWaiting as exc:
            from tabularium.core.reply_envelope import Reply
            result = Reply(exc.decision['question'], complete=False, task_outcome='waiting')
            result.decision_id = exc.decision['id']
            messages.append({'role': 'assistant', 'content': result})
            agent.save_conversation(chat)
            return result
        except SessionLimit as exc:
            if getattr(agent, "_trial", None):
                from tabularium.core.reply_envelope import Reply
                return Reply("Trial effort allowance exhausted; unfinished deliverables remain unverified.", complete=False)
            agent.save_conversation(chat)
            after = completed_actions(messages)
            calls = {b.get("id") for m in messages if isinstance(m.get("content"), list)
                     for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_use"}
            pending = []
            for path in (agent.RUNTIME_DIR / "task_state" / "actions").glob("*.json"):
                record = json.loads(path.read_text(encoding="utf-8"))
                if record.get("status") == "started":
                    pending.append(str(path))
            if pending or calls - after:
                raise RuntimeError("Session ended with an unknown action outcome; inspect receipts before resuming.") from exc
            new_ids = after - before
            new_results = [
                b.get("content", "")
                for m in messages if isinstance(m.get("content"), list)
                for b in m["content"]
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in new_ids
            ]
            real_progress = any(not isinstance(c, (Refused, Failed, AlreadyShown)) for c in new_results)
            if not new_ids or not real_progress:
                raise RuntimeError("Session limit without completed tool progress; stopped instead of restarting a loop.") from exc
            sessions += 1
            atomic_json(agent.RUNTIME_DIR / "task_state" / "session_checkpoint.json",
                        {"reason": str(exc), "sessions": sessions, "messages": len(messages),
                         "completed_actions": sorted(after), "pending_actions": [],
                         "task_checkpoint": str(agent.RUNTIME_DIR / "checkpoint.json")})
            agent._inbox.record_event("session_boundary", str(exc), role="runtime")
            if (getattr(agent, "BACKGROUND_TASK_ID", "") and getattr(agent, "_background", None) is not None
                    and callable(getattr(agent, "load_memory_context", None))):
                ident = int(agent.BACKGROUND_TASK_ID)
                serialised = agent._serialise_messages(messages) if hasattr(agent, "_serialise_messages") else list(messages)
                agent._background.archive_episode(ident, serialised, str(exc))
                held = agent.loaded_skills(chat) if hasattr(agent, "loaded_skills") else []
                messages[:] = agent._background.episode_seed(ident, chat, agent.load_memory_context(), continuation=True)
                for attr in ("_repeat_calls", "_episode_reads", "_window_anchor"):
                    if hasattr(agent, attr):
                        getattr(agent, attr).pop(chat, None)
                if held:
                    agent.carry_skills(chat, messages, held)
                agent.save_conversation(chat)
