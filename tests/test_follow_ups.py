"""Two bubbles are one thought, not two tasks (2026-10-16).

Why: the inbox treated every Telegram message as a job. Alex wrote [quote removed] and, a minute later, "随着结果的变化再调整";
the second arrived while Cato was answering the first, was queued as a separate task and got a canned "Saved and
queued" receipt. Now a text message sent during a turn joins that turn at the next tool boundary, bubbles typed within
three seconds become one message, and the receipt only appears when something is genuinely third in line."""
import contextlib, io, json, tempfile, time, unittest
from pathlib import Path
from unittest.mock import Mock
from agent_harness import make_agent
from tabularium.channels.owner_telegram import OWNER_CHAT_ID, OWNER_USER_ID
CHAT = OWNER_CHAT_ID


def telegram(message_id, text):
    return {"message_id": message_id, "date": int(time.time()), "text": text,
            "from": {"id": OWNER_USER_ID, "is_bot": False, "first_name": "H"}, "chat": {"id": CHAT, "type": "private"}}


def insert(inbox, ident, text, status="pending", created=None):
    with inbox.db() as db:
        db.execute("INSERT INTO jobs(id,chat,message,handler,payload,status,created) VALUES (?,?,?,?,?,?,?)",
                   (ident, CHAT, ident, "handle_message", json.dumps(telegram(ident, text)), status, created or time.time()))


def status(inbox, ident):
    with inbox.db() as db:
        return db.execute("SELECT status FROM jobs WHERE id=?", (ident,)).fetchone()[0]


class FollowUps(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "cato"; root.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            self.agent = make_agent("cato", root)
        self.agent.BACKGROUND_TASK_ID = ""
        self.inbox = self.agent._inbox

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_message_sent_during_a_turn_joins_it_at_the_next_boundary(self):
        insert(self.inbox, 1, "可以试试", status="running"); insert(self.inbox, 2, "随着结果的变化再调整")
        self.inbox.local.job = 1
        messages = [{"role": "user", "content": "[2026-10-16 Friday 15:33] 可以试试"}]; api = list(messages)
        self.agent.conversations[CHAT] = messages
        fresh = self.agent._background_boundary(messages, api, CHAT)
        self.assertEqual(len(fresh), 1)
        self.assertTrue(fresh[0]["content"].endswith("随着结果的变化再调整"), fresh)
        self.assertIs(messages[-1], fresh[0]); self.assertIs(api[-1], fresh[0])
        self.assertEqual(status(self.inbox, 2), "done")                       # never run as its own task
        self.assertEqual(self.agent._background_boundary(messages, api, CHAT), [])
        saved = json.loads(self.agent._conversation_path(CHAT).read_text(encoding="utf-8"))
        self.assertTrue(saved[-1]["content"].endswith("随着结果的变化再调整"))   # saved before the job was marked done

    def test_only_messages_after_the_running_job_and_only_text(self):
        insert(self.inbox, 5, "earlier, still pending", created=time.time() - 100)
        insert(self.inbox, 6, "running", status="running"); insert(self.inbox, 7, "later")
        self.inbox.local.job = 6
        fresh = self.agent._background_boundary([], [], CHAT)
        self.assertEqual([e["content"][-5:] for e in fresh], ["later"])
        self.assertEqual((status(self.inbox, 5), status(self.inbox, 7)), ("pending", "done"))

    def test_bubbles_typed_within_three_seconds_are_one_message(self):
        from telebot.types import Message
        now = time.time()
        insert(self.inbox, 1, "可以试试", created=now - 5); insert(self.inbox, 2, "随着结果的变化再调整", created=now - 4)
        first = Message.de_json(telegram(1, "可以试试"))
        ids, merged = self.inbox.gather(CHAT, 1, now - 5, first)
        self.assertEqual((ids, merged.text), ([1, 2], "可以试试\n随着结果的变化再调整"))

    def test_receipt_only_when_third_in_line(self):
        self.inbox.send = Mock()
        accept = self.inbox.handler(lambda message: None)
        from telebot.types import Message
        insert(self.inbox, 1, "running", status="running")
        accept(Message.de_json(telegram(2, "second")))
        self.inbox.send.assert_not_called()                                   # queued quietly: it will join the turn
        accept(Message.de_json(telegram(3, "third")))
        self.inbox.send.assert_called_once()
        self.assertIn("queued behind", self.inbox.send.call_args.args[1])

    def test_background_workers_take_steers_not_follow_ups(self):
        insert(self.inbox, 1, "running", status="running"); insert(self.inbox, 2, "follow-up")
        self.inbox.local.job = 1
        self.agent.BACKGROUND_TASK_ID = "1"
        self.assertEqual(self.agent._background_boundary([], [], CHAT), [])
        self.assertEqual(status(self.inbox, 2), "pending")


if __name__ == "__main__":
    unittest.main()
