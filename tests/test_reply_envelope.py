"""The reply is a declared field on every route; nothing outside it reaches Alex (2026-10-12).

At 19:27 Cato's last text was his working notes and the runtime sent it, because a turn ended with "whatever text
came last". Each route now requests a structured final answer with one `reply` field through the provider's own
structured-output option, and delivers only that field. A final answer without it stops the turn; it is not sent."""
import sys, tempfile, unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock
ROOT = Path(__file__).resolve().parents[1]
from tabularium.core import reply_envelope as env
from agent_harness import load_agent

NOTES = "Alex says restarting PyCharm didn't help. Why the difference? Let me think about teardown order..."


class Envelope(unittest.TestCase):
    def test_reply_is_taken_from_the_field_only(self):
        self.assertEqual(env.reply('{"reply": "收盘价是 231.40 美元。"}'), "收盘价是 231.40 美元。")
        self.assertEqual(env.reply({"reply": " OK "}), "OK")

    def test_modality_is_declared_not_tagged(self):
        self.assertEqual(env.reply('{"reply": "好的", "modality": "voice"}').modality, "voice")
        self.assertEqual(env.reply('{"reply": "好的"}').modality, "text")                 # absent: text
        self.assertEqual(env.reply({"reply": "x", "modality": "song"}).modality, "text")  # unknown: text
        self.assertTrue(env.reply('{"reply": "好的"}').complete)
        cut = env.Reply("I hit my step limit", complete=False)
        self.assertEqual((cut, cut.complete, cut.modality), ("I hit my step limit", False, "text"))
        self.assertIn("modality", env.SCHEMA["required"])

    def test_work_is_declared(self):
        self.assertEqual(env.reply('{"reply": "x", "work": "started"}').work, "started")
        self.assertEqual(env.reply('{"reply": "x"}').work, "none")
        self.assertEqual(env.reply({"reply": "x", "work": "maybe"}).work, "none")
        self.assertTrue(env.unbacked(env.reply('{"reply": "x", "work": "started"}'), started=False))
        self.assertFalse(env.unbacked(env.reply('{"reply": "x", "work": "started"}'), started=True))
        self.assertFalse(env.unbacked(env.reply('{"reply": "x", "work": "awaiting_go"}'), started=False))
        self.assertIn("work", env.SCHEMA["required"])

    def test_text_outside_the_field_is_never_a_reply(self):
        for bad in (NOTES, "", '{"answer": "x"}', '{"reply": "   "}', None):
            with self.assertRaises(env.ReplyMissing):
                env.reply(bad)

    def test_one_schema_feeds_every_provider(self):
        self.assertIs(env.CLAUDE_SDK_OUTPUT_FORMAT["schema"], env.SCHEMA)
        self.assertIs(env.ANTHROPIC_OUTPUT_CONFIG["format"]["schema"], env.SCHEMA)
        self.assertIs(env.OPENAI_TEXT["format"]["schema"], env.SCHEMA)


class Routes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "cato"; root.mkdir()
        self.runtime = load_agent("cato", root)["runtime"]
        self.runtime.BACKGROUND_TASK_ID = ""

    def tearDown(self):
        self.tmp.cleanup()

    def anthropic(self, final_text):
        sent = []
        response = NS(content=[NS(type="text", text=final_text)], stop_reason="end_turn", usage=None)
        def stream(**kwargs):
            sent.append(kwargs)
            return nullcontext(NS(get_final_message=lambda: response))
        self.runtime.anthropic_client = NS(messages=NS(stream=Mock(side_effect=stream)))
        return sent

    def test_anthropic_route_requests_the_field_and_delivers_it(self):
        sent = self.anthropic('{"reply": "改到所有 import 的最前面再试一次。"}')
        messages = [{"role": "user", "content": "问题"}]
        self.assertEqual(self.runtime.run_turn_anthropic(messages, 1), "改到所有 import 的最前面再试一次。")
        self.assertEqual(sent[0]["output_config"], env.ANTHROPIC_OUTPUT_CONFIG)
        self.assertEqual(messages[-1], {"role": "assistant", "content": "改到所有 import 的最前面再试一次。"})

    def test_anthropic_route_sends_nothing_when_the_field_is_missing(self):
        self.anthropic(NOTES)
        messages = [{"role": "user", "content": "问题"}]
        with self.assertRaises(env.ReplyMissing):
            self.runtime.run_turn_anthropic(messages, 1)
        self.assertFalse(any(m["role"] == "assistant" for m in messages))   # the notes are not saved as a reply

    def test_openai_route_requests_the_field(self):
        captured = {}
        def create(**kwargs):
            captured.update(kwargs)
            return NS(output=[], status="completed")
        client = NS(responses=NS(create=create))
        self.runtime._responses_call(client, model="m", instructions="", input_items=[], tools=[], plan_mode=False)
        self.assertEqual(captured["text"], env.OPENAI_TEXT)

    def test_openai_route_sends_nothing_when_the_field_is_missing(self):
        def fake(client, **kwargs):
            item = NS(type="message", content=[NS(type="output_text", text=NOTES)])
            return NS(output=[item], status="completed", incomplete_details=None, usage=None), "", [], []
        self.runtime._responses_call = fake
        self.runtime._chatgpt_plan = NS(access_token=lambda: None)
        self.runtime.OPENAI_API_KEY = "test"
        messages = [{"role": "user", "content": "问题"}]
        with self.assertRaises(env.ReplyMissing):
            self.runtime.run_turn_openai(messages, 1)
        self.assertFalse(any(m["role"] == "assistant" for m in messages))


if __name__ == "__main__":
    unittest.main()
