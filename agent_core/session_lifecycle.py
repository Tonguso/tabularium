"""Session boundaries are not task completion. Continue only from durable, settled evidence."""
import json
from agent_runtime import atomic_json


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
            from agent_core.reply_envelope import Reply
            result = Reply(exc.decision['question'], complete=False, task_outcome='waiting')
            result.decision_id = exc.decision['id']
            messages.append({'role': 'assistant', 'content': result})
            agent.save_conversation(chat)
            return result
        except SessionLimit as exc:
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
            if not after - before:
                raise RuntimeError("Session limit without completed tool progress; stopped instead of restarting a loop.") from exc
            sessions += 1
            atomic_json(agent.RUNTIME_DIR / "task_state" / "session_checkpoint.json",
                        {"reason": str(exc), "sessions": sessions, "messages": len(messages),
                         "completed_actions": sorted(after), "pending_actions": [],
                         "task_checkpoint": str(agent.RUNTIME_DIR / "checkpoint.json")})
            agent._inbox.record_event("session_boundary", str(exc), role="runtime")
            # Same worker, same loop guards, same canonical history. No action is replayed here.
