"""An action that needs the owner's yes: parking it with Yes and No buttons, and running or cancelling it when he
taps one or replies to the ask.
"""
import json
from tabularium.core.outcome import Refused


class ParkedActions:
    """Part of AgentApp (tabularium.core.agent_app): methods moved here unchanged on 2026-11-24."""

    # Command autonomy: when auto mode is ON for a chat, execute_command runs without the yes/no gate.
    def _approved(self, tool: str, **fields) -> bool:
        """True only while the runtime runs the parked call Alex said yes to: same tool, and every field a gate
        checks equal to the parked inputs (the command gate checks the command)."""
        if not self._approving or self._approving[0] != tool:
            return False
        parked = self._approving[1]
        return all(parked.get(k) == v for k, v in fields.items())

    def _park(self, chat_id: int, tool: str, inputs: dict, text: str, decision_id=None):
        """A guarded call waits for Alex as a durable decision with its exact inputs (2026-10-21). Until then it
        waited 60 seconds in memory: a late yes counted as no and a restart forgot the question. Now it has no
        deadline, survives restarts, and his yes runs exactly this call once."""
        from tabularium.records import task_decisions
        from tabularium.core.reply_envelope import Reply
        try:
            row = task_decisions.park_action(self._background, chat_id, tool, inputs, text, decision_id)
        except ValueError as exc:
            return Refused(str(exc))
        ask = Reply(text + "\n\nTap Yes to run it, No to cancel.")
        ask.decision_id = row["id"]
        ask.buttons = True
        self.send_message(chat_id, ask)
        return Refused(f"Not done yet: parked for Alex's verdict as decision #{row['id']}. He has the exact action with "
                       "Yes and No buttons under it; his Yes runs it exactly as parked. Do not retry it or ask again; "
                       "tell him it waits for his tap.")

    def handle_press(self, press):
        """Alex's tap on Yes or No under an ask. Queued durably first, then acknowledged to Telegram."""
        from tabularium.records.outbox import PRESS
        named = PRESS.fullmatch(press.data or "")
        if named:
            self._inbox.accept_press(int(named.group(1)), named.group(2) == "yes")
        self.bot.answer_callback_query(press.id)

    def _retire_buttons(self, chat, decision):
        """A settled action's ask loses its buttons, so nothing on the screen offers a verdict that is over."""
        for message_id in json.loads(decision["message_ids"] or "[]"):
            try:
                self.bot.edit_message_reply_markup(chat, message_id, reply_markup=None)
            except Exception as exc:                              # cosmetic: a stale button settles nothing anyway
                print(f"[decision] buttons of #{decision['id']} stayed on message {message_id}: {type(exc).__name__}")

    @staticmethod
    def _action_brief(tool, outcome):
        """One line for the chat about a parked action's run; the record keeps the whole output (2026-11-12: a
        4,000-character PowerShell error dump reached Alex's phone as the answer to "Ok")."""
        if not outcome.startswith("Tool returned: "):
            return outcome[:600]
        body = outcome[len("Tool returned: "):]
        try:
            data = json.loads(body)
        except ValueError:
            return "Done. " + body[:300]
        if isinstance(data, dict) and "exit" in data:
            first = (data.get("stdout") or "").strip().splitlines()
            line = ("Ran it, exit " + str(data.get("exit")) + ("." if not first else ": " + first[0][:200]))
            if data.get("stderr", "").strip():
                line += " Errors: " + data["stderr"].strip().splitlines()[0][:200]
            return line
        return "Done. " + body[:300]

    def _run_parked(self, chat, decision, yes, source):
        """Alex's verdict on a parked action, carried out once: his no declines it, his yes runs the exact parked
        call with the gates told it is approved. Returns the recorded outcome, or None when the action was already
        settled (a second yes runs nothing). The one path for a verdict however it arrives: as its own turn
        (_settle_by_reply) or inside the turn that asked (_settle_follow_up)."""
        from tabularium.records import task_decisions as td
        action = json.loads(decision["action"])
        if not yes:
            if not td.settle_action(self._background, chat, decision["id"], False, source):
                return None
            outcome = "Cancelled; it was not run."
        elif not td.settle_action(self._background, chat, decision["id"], True, source, "running"):
            return None
        else:
            self._approving = (action["tool"], action["inputs"])
            try:
                result = self.execute_tool(action["tool"], action["inputs"], chat)
                outcome = ("Not completed. " if self.unproductive(result) else "Tool returned: ") + str(result)[:4000]
            except Exception as exc:
                outcome = f"Execution interrupted; outcome unknown. {type(exc).__name__}: {exc}"
            finally:
                self._approving = None
        td.finish_action(self._background, chat, decision["id"], outcome)
        self._retire_buttons(chat, decision)
        return outcome

    def _settle_follow_up(self, chat, message):
        """A verdict on a parked action that arrives while the turn that asked is still running is carried out here,
        inside the turn (2026-11-15 14:41: the turn parked a desktop grant and sent the ask; Alex's "Yes" six seconds
        later joined the turn as text, the model could only ask again). The verdict names its action: a tap on the
        ask's button, or a plain yes or no sent as a reply to the ask. Returns the history entry that stands for it,
        or None when the message is an ordinary follow-up."""
        from tabularium.records import task_decisions as td
        text = (getattr(message, "text", None) or "").strip()
        yes = td.verdict(text)
        pressed = getattr(message, "decision_id", None) if getattr(message, "channel", "") == "button" else None
        ref = getattr(message, "reply_to_message", None)
        if pressed:
            decision = next((d for d in td.pending(self._background, chat, kind="action") if d["id"] == pressed), None)
            if decision is None:
                return f"[Alex tapped {'Yes' if yes else 'No'} on parked action #{pressed}, which was already settled. Nothing ran.]"
        elif yes is not None and ref is not None:
            decision = td.by_message(self._background, chat, ref.message_id)
            if not decision or decision["kind"] != "action":
                return None
        else:
            return None
        source = json.dumps({"message_id": message.message_id, "quote": text[:500]}, ensure_ascii=False)
        outcome = self._run_parked(chat, decision, yes, source)
        tool = json.loads(decision["action"])["tool"]
        if pressed:
            return (f"[Alex tapped {'Yes' if yes else 'No'} on parked action #{decision['id']} ({tool}). "
                    f"{outcome or 'It was already settled; nothing ran.'}]")
        if outcome is None:
            return None
        return (f"{self.compose_turn(message, nudge=False)}\n[Parked action #{decision['id']} ({tool}): "
                f"Alex said {'yes' if yes else 'no'}. {outcome}]")

    def _settle_by_reply(self, message):
        """A message that names a decision: a tap on an ask's Yes or No, a Telegram reply to the ask, the dashboard's
        reply to a question. A verdict on a parked action is carried out here; nothing else binds one, least of all a
        typed yes that names no ask (2026-11-23: guessing which ask a bare yes answered failed four times in a week).
        Returns (handled, note): handled means the message was a verdict on a parked action and no model turn is
        needed; note identifies a candidate question for attribution in the normal turn."""
        from tabularium.records import task_decisions as td
        chat = message.chat.id
        text = (getattr(message, "text", None) or "").strip()
        ref = getattr(message, "reply_to_message", None)
        from tabularium.learning import learning
        edit = getattr(message, 'skill_edit', None)               # the dashboard's buttons carry key and verdict as fields
        if edit is None:
            go = learning.GO.match(text)                          # typed by hand in Telegram
            edit = {'key': go.group(1), 'verdict': go.group(2)} if go else None
        if edit and self.config.role.learns:                      # a skill-edit verdict is settled in code, like a parked action
            try:
                outcome = learning.settle_go(self.AGENT_DIR, chat, edit['key'], edit['verdict'],
                                             self._recall.note(chat, text, "dashboard"), text, self._background, self.SYSTEM_PROMPT)
            except ValueError as exc:
                outcome = f"Not done: {exc}"
            self.send_message(chat, outcome)
            self.conversations.setdefault(chat, self.load_memory_context())
            self.conversations[chat] += [{"role": "user", "content": f"{self._ts(message)} {text}"},
                                         {"role": "assistant", "content": f"[{outcome}]"}]
            self.save_conversation(chat)
            return True, ""
        channel = getattr(message, 'channel', '')
        declared = getattr(message, 'decision_id', None) if channel in ('dashboard', 'button') else None
        decision = next((d for d in td.pending(self._background, chat) if d['id'] == declared), None) if declared else (td.by_message(self._background, chat, ref.message_id) if ref is not None else None)
        if declared and decision is None:
            if channel == 'dashboard':
                self.send_message(chat, 'That question is no longer pending. No action was repeated; refresh Messaging.')
            return True, ''                                       # a second tap on a settled ask runs nothing
        if decision is None:
            waiting = td.pending(self._background, chat, kind="action") if td.verdict(text) is not None else []
            if waiting:
                ids = ", ".join(f"#{d['id']}" for d in waiting)
                return False, (f"\n[Parked action {ids} still waits for Alex. This message names no ask, so it ran and "
                               "cancelled nothing: his verdict is a tap on Yes or No under the ask, or a reply to it. "
                               "If he meant this as that verdict, tell him so in one line.]")
            return False, ""
        source = json.dumps({"message_id": message.message_id, "quote": text[:500]}, ensure_ascii=False)
        if decision["kind"] == "action":
            yes = td.verdict(text)
            if yes is None:
                return False, (f"\n[This message replies to parked action #{decision['id']} without a plain yes or no; "
                               "it still waits. Ask him plainly whether to run it.]")
            action = json.loads(decision["action"])
            outcome = self._run_parked(chat, decision, yes, source)
            if outcome is None:
                return True, ""                                   # already settled: a second yes runs nothing
            if yes and action["tool"] == "desktop" and outcome.startswith("Tool returned"):
                # A desktop grant is for the work that follows: his yes is the message that starts that turn.
                return False, f"\n[Parked action #{decision['id']} (desktop take): Alex said yes. {outcome}]"
            self.send_message(chat, self._action_brief(action["tool"], outcome))
            # The agent sees what happened on its next turn.
            self.conversations.setdefault(chat, self.load_memory_context())
            self.conversations[chat] += [{"role": "user", "content": f"{self._ts(message)} {text}"},
                                         {"role": "assistant", "content": f"[Parked action #{decision['id']} "
                                          f"({action['tool']}): Alex said {'yes' if yes else 'no'}. {outcome}]"}]
            self.save_conversation(chat)
            return True, ""
        return False, (f"\n[This message replies to decision #{decision['id']}: {decision['question']}. "
                       "It is a candidate answer, not recorded approval. If the user's words resolve this question, "
                       "use task_decision answer with this decision_id, the answer and exact user quote. "
                       "Clarifications, new requests and unrelated replies leave it pending.]")
