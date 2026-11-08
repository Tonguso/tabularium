"""A reply quotes the message it answers when Alex has written something else since (2026-10-20).

Why: agents never used Telegram's reply-to. With several threads running at once, an answer that arrives after a
later message reads as an answer to the wrong one. Alex quotes when he replies; now so do they, and only then."""
import contextlib, io, json, tempfile, time, unittest
from pathlib import Path
from unittest.mock import Mock
from agent_harness import make_agent
from owner_telegram import OWNER_CHAT_ID, OWNER_USER_ID
CHAT = OWNER_CHAT_ID


def telegram(message_id, text):
    return {"message_id": message_id, "date": int(time.time()), "text": text,
            "from": {"id": OWNER_USER_ID, "is_bot": False, "first_name": "H"}, "chat": {"id": CHAT, "type": "private"}}


class ReplyQuoting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "cato"; root.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            self.agent = make_agent("cato", root)
        self.inbox = self.agent._inbox
        self.agent.bot = Mock()

    def tearDown(self):
        self.inbox.close(); self.tmp.cleanup()

    def insert(self, message_id, text):
        with self.inbox.db() as db:
            db.execute("INSERT INTO jobs(chat,message,handler,payload,status,created) VALUES (?,?,?,?,?,?)",
                       (CHAT, message_id, "handle_message", json.dumps(telegram(message_id, text)), "pending", time.time()))

    def kwargs(self):
        return self.agent.bot.send_message.call_args.kwargs

    def test_latest_message_is_answered_without_a_quote(self):
        self.insert(10, "What is the yen doing?")
        self.inbox.local.message_id = 10
        self.agent.send_message(CHAT, "Down 1%.")
        self.assertIsNone(self.kwargs().get("reply_to_message_id"))

    def test_answer_quotes_when_a_later_message_exists(self):
        self.insert(10, "What is the yen doing?")
        self.insert(11, "Also book the dentist")
        self.inbox.local.message_id = 10
        self.agent.send_message(CHAT, "Down 1%.")
        self.assertEqual(self.kwargs().get("reply_to_message_id"), 10)

    def test_background_result_quotes_its_request_only_when_superseded(self):
        self.insert(20, "Research ARXS")
        self.inbox.local.message_id = None
        self.agent.send_message(CHAT, "Here is the ARXS note.", reply_to=20)
        self.assertIsNone(self.kwargs().get("reply_to_message_id"))
        self.insert(21, "Dinner at 8?")
        self.agent.send_message(CHAT, "Here is the ARXS note.", reply_to=20)
        self.assertEqual(self.kwargs().get("reply_to_message_id"), 20)

    def test_deleted_original_falls_back_to_a_plain_send(self):
        self.insert(10, "x"); self.insert(11, "y")
        self.inbox.local.message_id = 10
        calls = []
        def send(chat, text, **kw):
            calls.append(kw)
            if kw.get("reply_to_message_id"):
                raise RuntimeError("message to be replied not found")
        self.agent.bot.send_message = send
        self.assertTrue(self.agent.send_message(CHAT, "Down 1%."))
        self.assertEqual([c.get("reply_to_message_id") for c in calls], [10, None])


if __name__ == "__main__":
    unittest.main()
