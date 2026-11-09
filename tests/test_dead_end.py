"""A reply cannot leave Alex's request silently undone (2026-10-21).

19:08 [quote removed]: Cato called generate_image, the image service refused, and his reply reported the refusal in words
that sounded provisional while nothing was running and nothing was asked. At 19:29 Alex asked [quote removed] and learned
he had stopped. The reply now declares where the request stands; 'blocked' is sent back once for another route
(here the logo skill's own SVG method) or a question."""
import contextlib, io, json, tempfile, unittest
from pathlib import Path

from agent_harness import make_agent
from tabularium.core import reply_envelope
from tabularium.core.turn_loop import run, Exchange
from tabularium.channels.owner_telegram import OWNER_CHAT_ID
CHAT = OWNER_CHAT_ID


def envelope(text, request):
    return json.dumps({"reply": text, "modality": "text", "work": "none", "answers": [],
                       "request": request}, ensure_ascii=False)


class DeadEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / "coordinator"; root.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            self.agent = make_agent("coordinator", root)
        self.agent.BACKGROUND_TASK_ID = ""
        self.agent._background.activated_since = lambda chat, since: False

    def turn(self, replies):
        seen = []
        class Provider:
            calls = 0
            def exchange(self, history):
                seen.append(json.dumps(history[-1], ensure_ascii=False))
                self.calls += 1
                return Exchange(text=replies[min(self.calls, len(replies)) - 1])
        provider = Provider()
        out = run(self.agent, [{"role": "user", "content": "继续画图吧"}], CHAT, provider)
        return out, provider.calls, seen

    def test_a_blocked_request_gets_one_more_try(self):
        out, calls, seen = self.turn([envelope("图像服务拦截了请求，这次没生成。", "blocked"),
                                      envelope("改用 SVG 画好了，见图。", "done")])
        self.assertEqual((str(out), calls), ("改用 SVG 画好了，见图。", 2))
        self.assertIn("another route", seen[1])

    def test_an_honest_stop_is_delivered_after_the_one_retry(self):
        out, calls, _ = self.turn([envelope("被拦截了。", "blocked"), envelope("我停在这里：两种方式都被拒绝。", "blocked")])
        self.assertEqual((str(out), calls), ("我停在这里：两种方式都被拒绝。", 2))

    def test_done_asking_and_conversation_pass_straight_through(self):
        for state in ("done", "asking", "none"):
            out, calls, _ = self.turn([envelope("ok", state)])
            self.assertEqual(calls, 1, state)

    def test_the_field_is_declared_and_parsed(self):
        self.assertIn("request", reply_envelope.SCHEMA["required"])
        self.assertEqual(reply_envelope.reply({"reply": "x", "request": "blocked"}).request, "blocked")
        self.assertEqual(reply_envelope.reply({"reply": "x", "request": "maybe"}).request, "none")


if __name__ == "__main__":
    unittest.main()
