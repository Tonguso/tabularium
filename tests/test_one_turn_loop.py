"""One turn loop for the API routes; a provider only translates (2026-10-15).

Why: run_turn_openai and run_turn_anthropic each carried the whole turn: boundary, abort, context, truncation, tool
execution, budget warning, step limit, envelope, saving. Every fix this week was made twice (the reply envelope, the
cut-off note, the step limit); the reasoning leak took three fixes because there were three places to leak from.
Now agent_core.turn_loop.run owns the turn and asks a provider for one exchange at a time."""
import sys, tempfile, unittest
from copy import deepcopy
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
from agent_harness import make_agent
from agent_core import turn_loop
from agent_core.turn_loop import Exchange

ONCE = ("your response was CUT OFF", "tool rounds used in this session", "Cut off: my output hit")
FILES = [ROOT / "engine.py", ROOT / "subscription_runtime.py", *sorted((ROOT / "agent_core").glob("*.py"))]


class Scripted:
    """A provider that answers from a script of Exchange objects (or a str for an early reply)."""
    def __init__(self, script):
        self.script = list(script); self.seen = []; self.wire = []
    def exchange(self, api_messages):
        self.seen.append(len(api_messages))
        self.wire.append(deepcopy(api_messages))
        return self.script.pop(0)


def final(text): return Exchange(text='{"reply": "%s"}' % text)
def call(ident, name, inputs): return Exchange(blocks=[{"type": "tool_use", "id": ident, "name": name, "input": inputs}],
                                               calls=[(ident, name, inputs)])


class OneLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "cato"; root.mkdir()
        self.agent = make_agent("cato", root)
        self.agent.BACKGROUND_TASK_ID = ""
        self.agent.MAX_TOOL_ITERATIONS = 4; self.agent.TOOL_BUDGET_WARN_AT = 2
        self.ran = []
        self.agent._execute_tool_unguarded = lambda name, inputs, chat: self.ran.append((name, inputs)) or "tool said hi"

    def tearDown(self):
        self.tmp.cleanup()

    def run_loop(self, script, messages=None):
        messages = messages if messages is not None else [{"role": "user", "content": "hi"}]
        self.provider = Scripted(script)
        return turn_loop.run(self.agent, messages, 1, self.provider), messages

    def test_each_turn_rule_is_written_once(self):
        for literal in ONCE:
            hits = [f"{p.name}:{i}" for p in FILES for i, line in enumerate(p.read_text(encoding="utf-8-sig").splitlines(), 1)
                    if literal in line]
            self.assertEqual(len(hits), 1, (literal, hits))

    def test_tool_call_then_answer(self):
        reply, messages = self.run_loop([call("c1", "read_file", {"path": "x"}), final("OK")])
        self.assertEqual((reply, reply.complete, reply.modality), ("OK", True, "text"))
        self.assertEqual(self.ran, [("read_file", {"path": "x"})])
        self.assertEqual(messages[1], {"role": "assistant", "content": [{"type": "tool_use", "id": "c1", "name": "read_file", "input": {"path": "x"}}]})
        self.assertEqual(messages[2]["content"][0], {"type": "tool_result", "tool_use_id": "c1", "content": "tool said hi"})
        self.assertEqual(messages[3], {"role": "assistant", "content": "OK"})

    def test_budget_warning_once_then_step_limit(self):
        script = [call(str(i), "read_file", {"i": i}) for i in range(4)]
        messages = [{"role":"user", "content":"work"}]
        self.agent.conversations[1] = messages
        try:
            provider = Scripted(script)
            turn_loop.run(self.agent, messages, 1, provider)
            self.fail("Expected a session boundary, not a final reply")
        except turn_loop.SessionLimit:
            pass
        self.assertEqual(messages[-1]["role"], "user")
        warnings = [b for m in messages if isinstance(m.get("content"), list) for b in m["content"]
                    if b.get("type") == "text" and "tool rounds used" in b.get("text", "")]
        self.assertEqual(warnings, [])
        self.assertIn("tool rounds used", str(provider.wire[2]))
        self.assertEqual(len(self.ran), 4)

    def test_three_cut_offs_end_the_turn(self):
        cut = Exchange(text='{"reply": "partial"}', truncated=True)
        reply, messages = self.run_loop([cut, cut, cut])
        self.assertFalse(reply.complete); self.assertIn("three times", reply)
        notes = [m for m in messages if m["role"] == "user" and isinstance(m["content"], list) and "CUT OFF" in m["content"][0].get("text", "")]
        self.assertEqual(notes, [])                         # warnings are wire-only
        self.assertIn("CUT OFF", str(self.provider.wire[1]))
        self.assertIn("CUT OFF", str(self.provider.wire[2]))

    def test_unexpected_stop_is_reported_not_delivered_as_an_answer(self):
        reply, _ = self.run_loop([Exchange(stop="refusal")])
        self.assertEqual((str(reply), reply.complete), ("(stopped: refusal)", False))

    def test_early_reply_from_the_provider_is_saved_and_returned(self):
        reply, messages = self.run_loop(["Sign-in needs attention."])
        self.assertEqual((reply, messages[-1]), ("Sign-in needs attention.", {"role": "assistant", "content": "Sign-in needs attention."}))

    def test_a_promise_without_a_task_is_sent_back_once(self):
        promise = Exchange(text='{"reply": "我会把研究重点收窄到在线 change-point detection。", "work": "started"}')
        asks = Exchange(text='{"reply": "要我现在开始研究吗？", "work": "awaiting_go"}')
        reply, messages = self.run_loop([promise, asks])
        self.assertEqual((str(reply), reply.work), ("要我现在开始研究吗？", "awaiting_go"))
        self.assertNotIn("work=started", str(messages))
        self.assertIn("work=started", str(self.provider.wire[1]))
        self.assertEqual(messages[1]["role"], "assistant")
        self.assertFalse(any("收窄" in str(m["content"]) for m in messages))   # the empty promise was never delivered or saved

    def test_a_promise_backed_by_a_task_is_delivered(self):
        class Starts(Scripted):
            def exchange(self, api_messages):
                self.agent._background.start(1, "研究 change-point detection")
                return super().exchange(api_messages)
        provider = Starts([Exchange(text='{"reply": "已经开始研究。", "work": "started"}')]); provider.agent = self.agent
        reply = turn_loop.run(self.agent, [{"role": "user", "content": "那就开始研究吧"}], 1, provider)
        self.assertEqual((str(reply), reply.work), ("已经开始研究。", "started"))

    def test_abort_stops_before_the_next_exchange(self):
        self.agent._abort_requested[1] = True
        reply, messages = self.run_loop([final("never")])
        self.assertEqual((str(reply), reply.complete), ("Stopped.", False))


if __name__ == "__main__":
    unittest.main()
