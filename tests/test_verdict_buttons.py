"""A verdict on a parked action names the action (2026-11-23).

Until today a typed yes with no reply was bound by working out which ask it answered. 2026-11-12: "Ok" to a wording
question ran a deletion parked eleven hours earlier. 2026-11-14: three yeses to a desktop grant bound to nothing, and
each retry parked a duplicate. 2026-11-15: a yes during the asking turn joined it as text. Every fix refined the
guess. Now the ask carries Yes and No buttons that name its decision; a tap, or a reply to the ask, is the verdict,
and a typed yes that names no ask settles nothing."""
import contextlib, io, json, tempfile, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_harness import make_agent
from tabularium.channels.messaging import decode
from tabularium.channels.owner_telegram import OWNER_CHAT_ID, OWNER_USER_ID, owner_press
from tabularium.records import task_decisions
CHAT = OWNER_CHAT_ID
COMMAND = "Remove-Item -Recurse D:\\Baskets\\old"


def message(text):
    return SimpleNamespace(chat=SimpleNamespace(id=CHAT), text=text, message_id=int(time.time() * 1000) % 10**9,
                           date=int(time.time()), reply_to_message=None)


class Proc:
    pid, returncode = 1, 0

    def communicate(self, timeout=None):
        return b"removed", b""


class VerdictButtons(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / "coordinator"; root.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            self.agent = make_agent("coordinator", root)
        self.agent.BACKGROUND_TASK_ID = ""
        self.agent.get_auto_mode = lambda chat: False
        self.agent.bot = Mock()
        self.sent = []
        def send(chat, **kw):
            self.sent.append(kw)
            return SimpleNamespace(message_id=9000 + len(self.sent))
        self.agent.bot.send_message.side_effect = send
        self.ran = []
        patcher = patch("tabularium.core.agent_app.subprocess.Popen", side_effect=lambda argv, **kw: self.ran.append(argv[-1]) or Proc())
        patcher.start(); self.addCleanup(patcher.stop)

    def park(self):
        self.agent.execute_tool("execute_command", {"command": COMMAND}, CHAT)
        self.agent.outbox().flush(CHAT)
        return task_decisions.pending(self.agent._background, CHAT, kind="action")

    def press(self, decision, yes=True):
        job = self.agent._inbox.accept_press(decision, yes)
        with self.agent._inbox.db() as db:
            return job, decode(json.loads(db.execute("SELECT payload FROM jobs WHERE id=?", (job,)).fetchone()[0]))

    def test_the_ask_goes_out_with_buttons_that_name_its_decision(self):
        parked = self.park()[0]
        ask = self.sent[-1]
        self.assertIn("Tap Yes to run it, No to cancel.", ask["text"])
        data = [b.callback_data for row in ask["reply_markup"].keyboard for b in row]
        self.assertEqual(data, [f"decision:{parked['id']}:yes", f"decision:{parked['id']}:no"])
        self.agent.send_message(CHAT, "It waits for your tap.")                 # an ordinary message carries none
        self.agent.outbox().flush(CHAT)
        self.assertNotIn("reply_markup", self.sent[-1])

    def test_a_typed_yes_that_names_no_ask_runs_nothing_even_right_after_the_ask(self):
        parked = self.park()[0]
        handled, note = self.agent._settle_by_reply(message("yes"))
        self.assertFalse(handled)
        self.assertIn(f"#{parked['id']} still waits", note)
        self.assertEqual(self.ran, [])
        self.assertEqual(len(task_decisions.pending(self.agent._background, CHAT, kind="action")), 1)
        self.assertEqual(self.agent._settle_by_reply(message("Tell me about the weather")), (False, ""))

    def test_a_tap_runs_the_action_once_and_the_ask_loses_its_buttons(self):
        parked = self.park()[0]
        asked = json.loads(task_decisions.pending(self.agent._background, CHAT, kind="action")[0]["message_ids"])
        _, tap = self.press(parked["id"])
        self.assertEqual(self.agent._settle_by_reply(tap), (True, ""))
        self.assertEqual(self.ran, [COMMAND])
        self.assertEqual(task_decisions.pending(self.agent._background, CHAT, kind="action"), [])
        self.agent.bot.edit_message_reply_markup.assert_called_once_with(CHAT, asked[0], reply_markup=None)
        self.assertEqual(self.sent[-1]["text"], "Ran it, exit 0: removed")      # the outcome reaches Telegram, not the dashboard
        _, again = self.press(parked["id"])
        self.assertEqual(self.agent._settle_by_reply(again), (True, ""))        # a second tap: nothing runs, nothing is said
        self.assertEqual(self.ran, [COMMAND])

    def test_a_tap_on_no_cancels_without_running(self):
        parked = self.park()[0]
        _, tap = self.press(parked["id"], yes=False)
        self.assertEqual(self.agent._settle_by_reply(tap), (True, ""))
        self.assertEqual(self.ran, [])
        self.assertEqual(task_decisions.pending(self.agent._background, CHAT, kind="action"), [])

    def test_an_identical_action_is_asked_again_not_parked_twice(self):
        first = self.park()
        again = self.park()
        self.assertEqual([d["id"] for d in again], [first[0]["id"]])

    def test_a_tap_during_the_turn_that_asked_runs_the_action_inside_that_turn(self):
        # 2026-11-15 14:41: the turn parked a grant and sent the ask; "Yes" came six seconds later and joined the
        # still-running turn as text, so the model could only ask again.
        self.agent._inbox.local.job = 61
        with self.agent._inbox.db() as db:
            payload = {"message_id": 61, "date": int(time.time()), "text": "赶紧继续", "chat": {"id": CHAT, "type": "private"},
                       "from": {"id": OWNER_USER_ID, "is_bot": False, "first_name": "H"}}
            db.execute("INSERT INTO jobs(id,chat,message,handler,payload,status,created) VALUES (61,?,61,'handle_message',?,'running',?)",
                       (CHAT, json.dumps(payload), time.time()))
        self.agent._inbox.local.turn_messages = [(61, "赶紧继续")]
        parked = self.park()[0]
        job, _ = self.press(parked["id"])
        self.assertEqual(self.agent._inbox.follow_ups(CHAT, 61), [])            # never folded into a typed bubble
        messages = [{"role": "user", "content": "[2026-11-15 Sunday 14:40] 赶紧继续"}]; api = list(messages)
        self.agent.conversations[CHAT] = messages
        fresh = self.agent._background_boundary(messages, api, CHAT)
        self.assertEqual(self.ran, [COMMAND])
        self.assertEqual(len(fresh), 1)
        self.assertIn(f"[Alex tapped Yes on parked action #{parked['id']} (execute_command). Tool returned: ", fresh[0]["content"])
        self.assertEqual(self.agent._inbox.turn_messages(), [(61, "赶紧继续")])   # a tap is carried out, not a message to answer
        with self.agent._inbox.db() as db:
            self.assertEqual(db.execute("SELECT status FROM jobs WHERE id=?", (job,)).fetchone()[0], "running")

    def test_only_the_owners_tap_in_his_own_chat_is_taken(self):
        tap = lambda user, chat: SimpleNamespace(from_user=SimpleNamespace(id=user), message=SimpleNamespace(chat=SimpleNamespace(id=chat)))
        self.assertTrue(owner_press(tap(OWNER_USER_ID, OWNER_CHAT_ID)))
        self.assertFalse(owner_press(tap(7, OWNER_CHAT_ID)))
        self.assertFalse(owner_press(tap(OWNER_USER_ID, 7)))
        self.assertFalse(owner_press(None))

    def test_a_tap_is_queued_before_it_is_acknowledged_and_other_button_data_is_ignored(self):
        parked = self.park()[0]
        self.agent.handle_press(SimpleNamespace(id="q1", data=f"decision:{parked['id']}:yes"))
        self.agent.handle_press(SimpleNamespace(id="q2", data="decision:1:maybe"))
        with self.agent._inbox.db() as db:
            rows = db.execute("SELECT payload FROM jobs WHERE handler='handle_press'").fetchall()
        self.assertEqual([json.loads(r[0])["decision_id"] for r in rows], [parked["id"]])
        self.assertEqual([c.args for c in self.agent.bot.answer_callback_query.call_args_list], [("q1",), ("q2",)])


if __name__ == "__main__":
    unittest.main()
