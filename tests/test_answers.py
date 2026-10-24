"""A reply says which of Alex's messages it answers (2026-10-21).

17:44-17:45 Alex sent three messages: Cal should study how academia does it; or how mature quant strategies do
it; is Vit's app done. The first started Cato's turn, the other two joined it. Cato's reply answered the third only
and quoted the first. Now the reply lists what it answers; a message left out bounces the reply once, and the quote
goes on the answered message only when it is not Alex's latest."""
import contextlib, io, json, tempfile, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from agent_harness import make_agent
from agent_core import reply_envelope
from agent_core.reply_envelope import Reply
from agent_core.turn_loop import run, Exchange
from owner_telegram import OWNER_CHAT_ID, OWNER_USER_ID
CHAT = OWNER_CHAT_ID
TURN = [(11946, "Cal是不是应该去研究学术界是怎么做的"), (11947, "或者说，成熟的量化交易策略是怎么做的"),
        (11948, "Vit的app做好了吗")]


class Answers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / "cato"; root.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            self.agent = make_agent("cato", root)
        self.agent.BACKGROUND_TASK_ID = ""
        self.agent.bot = Mock()
        inbox = self.agent._inbox
        with inbox.db() as db:
            for mid, text in TURN:
                db.execute("INSERT INTO jobs(chat,message,handler,payload,status,created) VALUES (?,?,?,?,?,?)",
                           (CHAT, mid, "handle_message", json.dumps({"message_id": mid, "text": text}), "done", time.time()))
        inbox.local.message_id = 11946
        inbox.local.turn_messages = list(TURN)

    def quoted(self, reply):
        self.agent.send_message(CHAT, reply)
        return self.agent.bot.send_message.call_args.kwargs.get("reply_to_message_id")

    def test_the_answer_to_the_latest_message_is_not_quoted(self):
        self.assertIsNone(self.quoted(Reply("界面有了，端到端还没过关。", answers=["Vit的app"])))

    def test_an_answer_to_an_earlier_message_quotes_it(self):
        self.assertEqual(self.quoted(Reply("对，已让 Cal 改查学术文献。", answers=["Cal是不是应该"])), 11946)

    def test_a_reply_covering_all_three_is_not_quoted(self):
        self.assertIsNone(self.quoted(Reply("…", answers=["Cal是不是", "或者说，成熟的", "Vit的app"])))

    def test_coverage_is_checked_only_when_the_turn_took_in_more_than_one(self):
        missing = reply_envelope.unanswered(Reply("x", answers=["Vit的app"]), TURN)
        self.assertEqual([mid for mid, _ in missing], [11946, 11947])
        self.assertEqual(reply_envelope.unanswered(Reply("x", answers=["Cal 是不是", "或者说 成熟", "vit的APP"]), TURN), [])
        self.assertEqual(reply_envelope.unanswered(Reply("x"), TURN[:1]), [])

    def test_the_turn_bounces_once_when_messages_are_left_out(self):
        self.agent._background.activated_since = lambda chat, since: False
        seen = []
        class Provider:
            calls = 0
            def exchange(self, history):
                self.calls += 1
                seen.append(json.dumps(history[-1], ensure_ascii=False))
                answers = ["Vit的app"] if self.calls == 1 else ["Cal是不是", "或者说", "Vit的app"]
                return Exchange(text=json.dumps({"reply": f"reply {self.calls}", "modality": "text", "work": "none",
                                                 "answers": answers}, ensure_ascii=False))
        provider = Provider()
        messages = [{"role": "user", "content": TURN[0][1]}]
        out = run(self.agent, messages, CHAT, provider)
        self.assertEqual((str(out), provider.calls), ("reply 2", 2))
        self.assertIn("does not cover", seen[1])
        self.assertIn("或者说，成熟的量化交易策略是怎么做的", seen[1])
        self.assertNotIn("Vit的app做好了吗", seen[1].split("does not cover")[1])


if __name__ == "__main__":
    unittest.main()
