"""A bare yes reaches the parked action it answers (2026-11-14 19:52: three yeses to a desktop grant bound to nothing,
because the turn's own reply followed the ask and each retry parked a duplicate)."""
import contextlib, io, json, tempfile, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_harness import make_agent
from tabularium.channels.owner_telegram import OWNER_CHAT_ID, OWNER_USER_ID
from tabularium.records import task_decisions
CHAT = 1


def message(text):
    return SimpleNamespace(chat=SimpleNamespace(id=CHAT), text=text, message_id=int(time.time() * 1000) % 10**9,
                           date=int(time.time()), reply_to_message=None)


class BareYes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / "coordinator"; root.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            self.agent = make_agent("coordinator", root)
        self.agent.BACKGROUND_TASK_ID = ""
        self.agent.get_auto_mode = lambda chat: False
        self.agent.bot = Mock()
        self.agent.bot.send_message.side_effect = lambda *a, **k: SimpleNamespace(message_id=int(time.time() * 1000) % 10**9)

    def park(self, command):
        self.agent.execute_tool("execute_command", {"command": command}, CHAT)
        self.agent.outbox().flush(CHAT)
        return task_decisions.pending(self.agent._background, CHAT, kind="action")

    def test_the_reply_that_goes_with_the_ask_does_not_unbind_a_bare_yes_but_a_later_turn_does(self):
        self.agent._inbox.local.job = 41
        parked = self.park("Remove-Item -Recurse D:\\Baskets\\old")[0]
        self.agent.send_message(CHAT, "It waits for your yes.")             # the same turn's reply, after the ask
        self.agent.outbox().flush(CHAT)
        self.assertTrue(self.agent.outbox().ask_is_current(CHAT, parked["id"]))
        self.agent._inbox.local.job = 42
        self.agent.send_message(CHAT, "Shall I start on the slides?")       # a later turn: the yes is about that
        self.agent.outbox().flush(CHAT)
        self.assertFalse(self.agent.outbox().ask_is_current(CHAT, parked["id"]))
        self.assertEqual(self.agent._settle_by_reply(message("ok")), (False, ""))

    def test_an_identical_action_is_asked_again_not_parked_twice_and_the_bare_yes_runs_it(self):
        self.agent._inbox.local.job = 51
        first = self.park("Remove-Item -Recurse D:\\Baskets\\old")
        again = self.park("Remove-Item -Recurse D:\\Baskets\\old")
        self.assertEqual([d["id"] for d in again], [first[0]["id"]])
        ran = []
        class Proc:
            pid, returncode = 1, 0
            def communicate(self, timeout=None):
                return b"removed", b""
        with patch("tabularium.core.agent_app.subprocess.Popen", side_effect=lambda argv, **kw: ran.append(argv[-1]) or Proc()):
            self.assertEqual(self.agent._settle_by_reply(message("yes")), (True, ""))
        self.assertEqual(ran, ["Remove-Item -Recurse D:\\Baskets\\old"])
        self.assertEqual(task_decisions.pending(self.agent._background, CHAT, kind="action"), [])

    def test_a_yes_that_arrives_during_the_turn_that_asked_runs_the_action_inside_that_turn(self):
        # 2026-11-15 14:41: the turn parked a desktop grant (#502) and sent the ask; "Yes" came six seconds later and
        # joined the still-running turn as text, so the model could only call task_decision (refused) and ask again.
        chat = OWNER_CHAT_ID                                                # follow-ups are the owner's messages only
        self.agent._inbox.local.job = 61
        with self.agent._inbox.db() as db:
            for ident, text, status in ((61, "赶紧继续", "running"), (62, "Yes", "pending")):
                payload = {"message_id": ident, "date": int(time.time()), "text": text, "chat": {"id": chat, "type": "private"},
                           "from": {"id": OWNER_USER_ID, "is_bot": False, "first_name": "H"}}
                db.execute("INSERT INTO jobs(id,chat,message,handler,payload,status,created) VALUES (?,?,?,?,?,?,?)",
                           (ident, chat, ident, "handle_message", json.dumps(payload), status, time.time()))
        self.agent.execute_tool("execute_command", {"command": "Remove-Item -Recurse D:\\Baskets\\old"}, chat)
        self.agent.outbox().flush(chat)
        parked = task_decisions.pending(self.agent._background, chat, kind="action")[0]
        messages = [{"role": "user", "content": "[2026-11-15 Sunday 14:40] 赶紧继续"}]; api = list(messages)
        self.agent.conversations[chat] = messages
        ran = []
        class Proc:
            pid, returncode = 1, 0
            def communicate(self, timeout=None):
                return b"removed", b""
        with patch("tabularium.core.agent_app.subprocess.Popen", side_effect=lambda argv, **kw: ran.append(argv[-1]) or Proc()):
            fresh = self.agent._background_boundary(messages, api, chat)
        self.assertEqual(ran, ["Remove-Item -Recurse D:\\Baskets\\old"])
        self.assertEqual(len(fresh), 1)
        self.assertIn("Yes", fresh[0]["content"])
        self.assertIn(f"[Parked action #{parked['id']} (execute_command): Alex said yes. Tool returned: ", fresh[0]["content"])
        self.assertIn('"stdout": "removed"', fresh[0]["content"])
        self.assertEqual(task_decisions.pending(self.agent._background, chat, kind="action"), [])
        with self.agent._inbox.db() as db:
            self.assertEqual(db.execute("SELECT status FROM jobs WHERE id=62").fetchone()[0], "done")   # never its own turn


if __name__ == "__main__":
    unittest.main()
