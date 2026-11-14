"""Strong language is a prompt to think, not a verdict (Alex, 2026-11-14: "when I use strong language, it's prudent to
think if this is something that needs to join the docket workflow"). The runtime spots it; the reply must say
correction or not_correction for each such message before it goes out; the judgement is recorded either way."""
import contextlib, io, json, tempfile, time, unittest
from pathlib import Path
from unittest.mock import Mock

from agent_harness import make_agent
from tabularium.core import reply_envelope
from tabularium.core.reply_envelope import Reply
from tabularium.core.turn_loop import run, Exchange
from tabularium.channels.owner_telegram import OWNER_CHAT_ID
CHAT = OWNER_CHAT_ID
TURN = [(12001, "Tiro is fucking stuck")]


class StrongLanguage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / "coordinator"; root.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            self.agent = make_agent("coordinator", root)
        self.agent.BACKGROUND_TASK_ID = ""
        self.agent.bot = Mock()
        inbox = self.agent._inbox
        with inbox.db() as db:
            for mid, text in TURN:
                db.execute("INSERT INTO jobs(chat,message,handler,payload,status,created) VALUES (?,?,?,?,?,?)",
                           (CHAT, mid, "handle_message", json.dumps({"message_id": mid, "text": text}), "done", time.time()))
        inbox.local.message_id = 12001
        inbox.local.turn_messages = list(TURN)

    def test_every_property_of_the_envelope_is_required_as_the_strict_schema_demands(self):
        # 2026-11-14 19:39: a property added without its required entry stopped every OpenAI turn with a 400.
        self.assertEqual(set(reply_envelope.SCHEMA["properties"]), set(reply_envelope.SCHEMA["required"]))

    def test_the_marker_is_a_list_of_the_owners_words(self):
        self.assertTrue(reply_envelope.strong_language("傻逼玩意儿"))
        self.assertFalse(reply_envelope.strong_language("Is it done?"))
        self.assertEqual(reply_envelope.unconsidered(Reply("x"), TURN), TURN)
        self.assertEqual(reply_envelope.unconsidered(Reply("x", correction=[12001]), TURN), [])
        self.assertEqual(reply_envelope.unconsidered(Reply("x", not_correction=[12001]), TURN), [])
        self.assertEqual(reply_envelope.unconsidered(Reply("x"), [(1, "Is it done?")]), [])

    def test_the_turn_bounces_once_until_the_agent_decides_and_the_decision_is_recorded(self):
        self.agent._background.activated_since = lambda chat, since: False
        seen = []
        class Provider:
            calls = 0
            def exchange(self, history):
                self.calls += 1
                seen.append(json.dumps(history[-1], ensure_ascii=False))
                body = {"reply": f"reply {self.calls}", "modality": "text", "work": "none", "answers": [12001]}
                if self.calls > 1:
                    body["not_correction"] = [12001]
                return Exchange(text=json.dumps(body, ensure_ascii=False))
        provider = Provider()
        out = run(self.agent, [{"role": "user", "content": TURN[0][1]}], CHAT, provider)
        self.assertEqual((str(out), provider.calls), ("reply 2", 2))
        self.assertIn("carry strong language", seen[1])
        self.assertEqual(out.not_correction, (12001,))
        self.assertEqual(reply_envelope.reply({"reply": "ok", "correction": [12001]}).not_correction, ())


if __name__ == "__main__":
    unittest.main()
