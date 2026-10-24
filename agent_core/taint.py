"""Taint: a deterministic rule for turns that have read untrusted content (2026-10-20).

Models do not reliably tell instructions from data, so a page, an email or a forwarded document can carry an
instruction the agent then follows. The frameworks' answer is a control outside the model, and this is the cheapest
one that fits here: once a turn has read external content, the actions that reach the outside world or the machine
need Alex's word. In the foreground that is the existing yes/no confirmation; a background worker cannot ask, so it
is refused and told to put the question in its checkpoint. A new message from Alex starts a clean foreground turn;
a background task stays tainted to its end, because what it read earlier still shapes what it does.

Guarded: send_email always; execute_command when the command is dangerous by the existing screen. Not guarded, by
choice: file writes inside the agent's own folders, deliveries to Alex, delegation briefs (Cal and Vit apply the same
rule in their own turns), so an ordinary research turn is unchanged.
"""
from agent_core.outcome import Refused

READERS = {"web_fetch", "read_email", "browser_navigate", "browser_screenshot", "browser_click", "browser_type",
           "read_research_reports", "list_research_reports", "search_conversations", "read_conversation_excerpt"}
GUARDED = {"send_email", "execute_command"}


def mark(agent, chat_id, tool):
    """Called after a tool ran: a reader taints the turn."""
    if tool in READERS:
        agent._tainted[chat_id] = tool


def reset(agent, chat_id):
    """A new foreground turn starts clean; a background task keeps its taint."""
    if not agent.BACKGROUND_TASK_ID:
        agent._tainted.pop(chat_id, None)


def check(agent, chat_id, tool, inputs):
    """Refused(...) when a guarded action needs a word Alex has not given; None to proceed."""
    source = agent._tainted.get(chat_id)
    if not source or tool not in GUARDED:
        return None
    if tool == "execute_command" and not agent._is_dangerous(inputs.get("command", "")):
        return None
    what = "send this email" if tool == "send_email" else "run this command"
    if agent.BACKGROUND_TASK_ID:
        return Refused(f"Not done: this task has read external content ({source}), so it cannot {what} on its own. "
                       "If the action is right, put it to Alex as a question in your checkpoint and stop.")
    if agent._approved(tool, **inputs):
        return None                         # Alex said yes to exactly this parked call
    summary = (f"to {inputs.get('to', '')}: {inputs.get('subject', '')}" if tool == "send_email"
               else f"`{inputs.get('command', '')[:300]}`")
    return agent._park(chat_id, tool, inputs,
                       f"This turn read external content ({source}) and now wants to {what}:\n{summary}")
