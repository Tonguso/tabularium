"""Three ways the agent could act on something other than the current state (Codex review, 2026-10-12).

1. An instruction sent to a running task was saved and marked received, but on the Claude subscription route the live
   session never saw it (task 144: the carriers basket). It now reaches the model inside the next tool result.
2. Memory was copied into the top of the conversation once per process; corrections were appended later and scrolled
   out of view while the stale copy stayed. The current core memory is now attached to the latest message of every
   request and never stored.
3. The memory exam ran without the agent's memory and only noted forbidden lookups. It now runs like a real turn,
   and the tools it may use are enforced in code."""
import sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
from agent_harness import load_agent


class SteerReachesTheLiveSession(unittest.TestCase):
    def test_instruction_arriving_mid_session_is_in_the_next_tool_result(self):
        import subscription_runtime as bridge
        import claude_agent_sdk as sdk
        with tempfile.TemporaryDirectory() as folder:
            update = {"role": "user", "content": "[Background scope update #102]\nAlso build a US carriers basket."}
            calls = iter([[], [update]])                      # nothing at session start; the steer arrives mid-session
            agent = NS(AGENT_DIR=Path(folder), RUNTIME_DIR=Path(folder), SYSTEM_PROMPT="persona", MAX_TOOL_ITERATIONS=4,
                       _abort_requested={}, _background_boundary=lambda *a: next(calls),
                       get_tools=lambda _: [{"name": "read_file", "description": "read",
                                             "input_schema": {"type": "object", "properties": {}}}],
                       _prepare_request_context=lambda m, c: m, _sanitise_tool_history=lambda m: m,
                       trim_history=lambda m, c: m, save_conversation=Mock(), execute_tool=Mock(return_value="towers saved"),
                       _inbox=NS(progress=Mock(), record_event=Mock()), _background=NS(newest=lambda c: 0, activated_since=lambda c, t: False), BACKGROUND_TASK_ID="")
            captured = {}

            def server(name, version, tools):
                captured["tools"] = tools
                return {}

            async def query(prompt, options):
                captured["result"] = await captured["tools"][0].handler({})
                yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                                        session_id="fake", result="", structured_output={"reply": "Done"})
            with patch.object(bridge, "find_cli", return_value="fake.exe"), \
                 patch.object(bridge, "auth_status", return_value={"loggedIn": True, "authMethod": "claude.ai"}), \
                 patch.object(sdk, "create_sdk_mcp_server", side_effect=server), patch.object(sdk, "query", side_effect=query):
                bridge.run_claude(agent, [{"role": "user", "content": "Build a US tower basket."}], 1, "claude-test")
            text = " ".join(b.get("text", "") for b in captured["result"]["content"])
            self.assertIn("towers saved", text)
            self.assertIn("Also build a US carriers basket.", text)     # in front of the running model, not only saved


class AMatterOutsideTheWindowArrivesWithTheMessage(unittest.TestCase):
    """2026-11-01 14:44: Alex asked for 'the buyer's survey'. A month of flat-purchase talk was in the archive, the
    newest turn 32 minutes before the window began, and Cato asked him who the buyer was."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "cato"; self.root.mkdir()
        self.runtime = load_agent("cato", self.root)["runtime"]
        self.runtime.BACKGROUND_TASK_ID = ""
        (self.root / "memory.md").write_text(
            "# Memory\n\n## Alex's Affairs (standing matters)\n\n- **Flat purchase, Galleria House 1204** "
            "[keywords: galleria, surveyor, christopher, 买方]: Alex is the buyer.\n", encoding="utf-8")
        self.runtime._recall.record(1, [
            {"role": "user", "content": "[2026-10-31 Saturday 14:20] 律师回邮件了"},
            {"role": "assistant", "content": "You said you would not exchange without a Level 2 survey; none is booked. "
                                             "Christopher at Jury O'Shea should check the HSBC offer expiry."},
            {"role": "user", "content": "[2026-10-31 Saturday 15:00] 帮我看看 Sperry 的鞋怎么保养"},
            {"role": "assistant", "content": "用绵羊油。"}], self.root / "conversation_1.json")

    def tearDown(self):
        self.tmp.cleanup()

    def wire(self, text):
        window = [{"role": "user", "content": "[2026-11-01 Sunday 07:49] 我们能否在家自建一个VPN"},
                  {"role": "assistant", "content": "可以。"},
                  {"role": "user", "content": f"[Incoming message 9]\n[2026-11-01 Sunday 14:44] {text}"}]
        wired = self.runtime._attach_current_context(window, 1)
        self.assertEqual(window[-1]["content"], f"[Incoming message 9]\n[2026-11-01 Sunday 14:44] {text}")  # history untouched
        return str(wired)

    def test_a_message_naming_the_matter_brings_its_newest_archived_turns(self):
        wire = self.wire("买方的那个survey你帮我找一个又快又好的")
        self.assertIn("What was last said about Flat purchase, Galleria House 1204", wire)
        self.assertIn("2026-10-31 14:20 you: You said you would not exchange", wire)
        self.assertNotIn("绵羊油", wire)

    def test_a_message_about_something_else_brings_nothing(self):
        self.assertNotIn("What was last said", self.wire("帮我看看 Sperry 的鞋怎么保养"))

    def test_turns_from_inside_the_window_are_not_repeated(self):
        self.runtime._recall.record(1, [{"role": "user", "content": "[2026-11-01 Sunday 10:00] 律师说 galleria 的事下周"},
                                        {"role": "assistant", "content": "好。"}], self.root / "conversation_1b.json")
        wire = self.wire("surveyor 找到了吗")
        self.assertIn("2026-10-31 14:20 you:", wire)
        self.assertNotIn("下周", wire)


class ARouteChangeRerunsTheBenchmarkOnce(unittest.TestCase):
    """2026-11-02 (the review article: rescore whenever the model changes). Until now a digest line said a rerun was
    due and nothing ran it."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "cato"; self.root.mkdir()
        (self.root.parent / "logs").mkdir()
        self.runtime = load_agent("cato", self.root)["runtime"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_one_launch_per_route_and_none_without_a_gap(self):
        from agent_core import regression
        with patch.object(regression, "primary_route", return_value="gpt-6-luna"), \
             patch.object(regression, "route_gap", return_value=None), patch("engine.subprocess.Popen") as popen:
            self.assertFalse(self.runtime.run_benchmark_rerun())
            popen.assert_not_called()
        with patch.object(regression, "primary_route", return_value="gpt-6-luna"), \
             patch.object(regression, "route_gap", return_value="- BENCHMARK? ..."), patch("engine.subprocess.Popen") as popen:
            self.assertTrue(self.runtime.run_benchmark_rerun())
            self.assertEqual(popen.call_args.args[0][2:4], ["eval", "cato"])
            self.assertFalse(self.runtime.run_benchmark_rerun())                 # the same route is not launched twice
            popen.assert_called_once()


class AnAnswerlessSessionGetsOneMoreSession(unittest.TestCase):
    def test_a_session_that_ends_without_a_reply_is_sent_back_once_then_stops(self):
        # 2026-10-31 04:02, Cato task 421: the subscription session ended with an empty result and no structured
        # output; the worker crashed with ReplyMissing and Alex's phone got "Worker error: ReplyMissing".
        import subscription_runtime as bridge
        import claude_agent_sdk as sdk
        from agent_core.reply_envelope import ReplyMissing
        with tempfile.TemporaryDirectory() as folder:
            agent = NS(AGENT_DIR=Path(folder), RUNTIME_DIR=Path(folder), SYSTEM_PROMPT="persona", MAX_TOOL_ITERATIONS=4,
                       _abort_requested={}, _background_boundary=lambda *a: [], get_tools=lambda _: [],
                       _prepare_request_context=lambda m, c: m, _sanitise_tool_history=lambda m: m,
                       trim_history=lambda m, c: m, save_conversation=Mock(), execute_tool=Mock(),
                       _inbox=NS(progress=Mock(), record_event=Mock()), _background=NS(newest=lambda c: 0, activated_since=lambda c, t: False), BACKGROUND_TASK_ID="")
            prompts = []

            def session(answers):
                async def query(prompt, options):
                    prompts.append(prompt)
                    yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                                            session_id="fake", result="", structured_output=answers.pop(0))
                return query
            patches = [patch.object(bridge, "find_cli", return_value="fake.exe"),
                       patch.object(bridge, "auth_status", return_value={"loggedIn": True, "authMethod": "claude.ai"}),
                       patch.object(sdk, "create_sdk_mcp_server", return_value={})]
            for p in patches: p.start()
            try:
                with patch.object(sdk, "query", side_effect=session([None, {"reply": "Done"}])):
                    self.assertEqual(bridge.run_claude(agent, [{"role": "user", "content": "Build it."}], 1, "claude-test"), "Done")
                self.assertEqual(len(prompts), 2)
                bounce = next(c for c in agent._inbox.record_event.call_args_list if c.args[:2] == ("bounce", "reply_missing"))
                self.assertIn("ended without a final answer", bounce.kwargs["details"]["warning"])
                with patch.object(sdk, "query", side_effect=session([None, None])):
                    self.assertRaises(ReplyMissing, bridge.run_claude, agent, [{"role": "user", "content": "Build it."}], 1, "claude-test")
            finally:
                for p in reversed(patches): p.stop()


class MemoryIsAlwaysCurrent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "cato"; self.root.mkdir()
        self.runtime = load_agent("cato", self.root)["runtime"]
        self.runtime.BACKGROUND_TASK_ID = ""

    def tearDown(self):
        self.tmp.cleanup()

    def write_memory(self, address):
        (self.root / "memory.md").write_text(f"# Memory\n\n## Alex\n- Home address: {address}\n", encoding="utf-8")

    def test_a_correction_is_in_force_on_the_next_request_and_nothing_stale_remains(self):
        rt = self.runtime
        self.write_memory("1 Old Street")
        messages = []
        rt._refresh_core_memory(messages, 1)
        messages.append({"role": "user", "content": "[2026-10-12 Monday 20:00] 我搬家了"})
        self.assertIn("1 Old Street", str(rt.trim_history(messages, 1)))
        self.write_memory("9 New Road")
        messages += [{"role": "assistant", "content": "已更新。"}]
        messages += [{"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}"} for i in range(80)]
        messages.append({"role": "user", "content": "[2026-10-12 Monday 21:00] 我住哪"})
        wire = str(rt.trim_history(messages, 1))
        self.assertIn("9 New Road", wire)
        self.assertNotIn("1 Old Street", wire)                       # no old copy anywhere in the request
        self.assertNotIn("Old Street", str(messages))                # memory is never stored in history
        self.assertNotIn("New Road", str(messages))


class ExamRunsLikeARealTurn(unittest.TestCase):
    def test_only_scoped_tools_exist_and_memory_is_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "cato"; root.mkdir()
            rt = load_agent("cato", root)["runtime"]          # the harness writes its own memory.md; replace it after
            (root / "memory.md").write_text("# Memory\n\n## Alex\n- Travels to Tokyo next week.\n", encoding="utf-8")
            rt.BACKGROUND_TASK_ID = ""
            seen = {}

            def fake_turn(messages, chat):
                seen["tools"] = {t.get("name") for t in rt.get_tools("anthropic")}
                seen["wire"] = str(rt.trim_history(messages, chat))
                seen["archive"] = rt._execute_tool_unguarded("search_conversations", {"query": "Tokyo"}, chat)
                return "Tokyo"
            rt.run_turn = fake_turn
            from agent_core import memory_exam
            answer, used = memory_exam.ask_agent(rt, 1, "Where does Alex travel next week?")
            self.assertEqual(seen["tools"], {"read_memory"})
            self.assertIn("not available", seen["archive"])            # refused by the dispatcher, not by a prompt
            self.assertIn("Tokyo next week", seen["wire"])             # the agent's usual memory is present
            self.assertIn("Core memory loaded", seen["wire"])          # the same opening as a real conversation
            self.assertIn("search_conversations", {t.get("name") for t in rt.get_tools("anthropic")})   # scope lifted after


if __name__ == "__main__":
    unittest.main()
