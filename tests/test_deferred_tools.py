"""Rarely used tools are listed in one line each and given in full when first reached for (2026-11-25).

Every request carried every tool definition: 56,000 characters for the coordinator on each model call, of which 46%
described tools that made 2.9% of its calls in thirty days. Those tools are now one tool, use_tool
(core/deferred_tools). The first call for a tool in a turn returns its instructions and runs nothing; the next runs
the tool as itself, so approvals, the boundary, action records and usage counts see the real tool."""
import json, tempfile, unittest
from pathlib import Path
from agent_harness import load_agent
from tabularium.core import context_window
from tabularium.core.deferred_tools import DEFERRED, USE_TOOL, called, fold
from tabularium.core.outcome import Refused


def agent(name="coordinator"):
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name) / name; root.mkdir()
    runtime = load_agent(name, root)["runtime"]
    runtime._cleanup = tmp
    return runtime


def names(tools):
    return [(t.get("function") or t)["name"] for t in tools]


class RequestToolList(unittest.TestCase):
    def test_deferred_tools_are_one_line_each_inside_use_tool(self):
        runtime = agent()
        sent, full = runtime.get_tools("anthropic"), runtime.available_tools("anthropic")
        folded = [n for n in names(full) if n in DEFERRED]
        self.assertGreater(len(folded), 15)
        self.assertEqual(names(sent), [n for n in names(full) if n not in DEFERRED] + [USE_TOOL])
        wrapper = sent[-1]
        self.assertEqual(wrapper["input_schema"]["properties"]["name"]["enum"], folded)
        for name in folded:
            self.assertIn(f"- {name}: ", wrapper["description"])
        self.assertLess(len(json.dumps(sent)), 0.65 * len(json.dumps(full)))
        for kept in ("read_file", "execute_command", "delegate", "task_checkpoint", "remember", "skill"):
            self.assertIn(kept, names(sent))

    def test_the_cache_marker_is_on_the_last_tool_sent_and_nowhere_else(self):
        runtime = agent()
        sent = runtime.get_tools("anthropic")
        self.assertIn("cache_control", sent[-1])
        self.assertFalse(any("cache_control" in t for t in sent[:-1]))
        self.assertFalse(any("cache_control" in t for t in runtime.available_tools("anthropic")))
        self.assertFalse(any("cache_control" in t for t in runtime.get_tools("anthropic")[:-1]))      # and not left behind by the call before

    def test_the_other_provider_gets_the_same_list_in_its_own_form(self):
        runtime = agent()
        sent = runtime.get_tools("openai")
        self.assertEqual(names(sent), names(runtime.get_tools("anthropic")))
        self.assertEqual(sent[-1]["type"], "function")
        self.assertEqual(sent[-1]["function"]["parameters"]["required"], ["name"])
        flat = runtime._oa_tools_for_responses()
        self.assertEqual(flat[-1]["name"], USE_TOOL)

    def test_an_agent_with_a_chosen_short_list_carries_it_in_full(self):
        runtime = agent("private")
        self.assertEqual(names(runtime.get_tools("anthropic")), names(runtime.available_tools("anthropic")))

    def test_a_scope_lists_only_the_deferred_tools_inside_it(self):
        runtime = agent()
        with runtime.tool_scope({"read_file", "verify_artifact"}):
            sent = runtime.get_tools("anthropic")
            self.assertEqual(names(sent), ["read_file", USE_TOOL])
            self.assertEqual(sent[-1]["input_schema"]["properties"]["name"]["enum"], ["verify_artifact"])
        with runtime.tool_scope({"read_file", "write_file"}):
            self.assertEqual(names(runtime.get_tools("anthropic")), ["read_file", "write_file"])

    def test_no_deferred_tool_means_no_use_tool(self):
        self.assertEqual(fold([{"name": "read_file"}]), [{"name": "read_file"}])


class Dispatch(unittest.TestCase):
    def setUp(self):
        self.runtime = agent()
        self.file = self.runtime.AGENT_DIR / "made.txt"
        self.file.write_text("made", encoding="utf-8")

    def records(self):
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted((self.runtime.RUNTIME_DIR / "task_state" / "actions").glob("*.json"))]

    def test_the_first_call_returns_the_instructions_and_runs_nothing(self):
        schema = next(t for t in self.runtime.available_tools("anthropic") if t["name"] == "verify_artifact")
        shown = self.runtime.execute_tool(USE_TOOL, {"name": "verify_artifact"}, 1)
        self.assertIn(schema["description"], shown)
        self.assertIn(json.dumps(schema["input_schema"], ensure_ascii=False), shown)
        self.assertIn("Nothing has run", shown)
        self.assertEqual(self.records(), [])

    def test_a_first_call_that_carries_input_is_not_run(self):
        shown = self.runtime.execute_tool(USE_TOOL, {"name": "verify_artifact", "input": {"path": str(self.file)}}, 1)
        self.assertIn("NOT run", shown)
        self.assertEqual(self.records(), [])

    def test_the_next_call_runs_the_tool_as_itself(self):
        self.runtime.execute_tool(USE_TOOL, {"name": "verify_artifact"}, 1)
        result = self.runtime.execute_tool(USE_TOOL, {"name": "verify_artifact", "input": {"path": str(self.file)}}, 1)
        self.assertIn("sha256", str(result))
        record = self.records()[-1]
        self.assertEqual((record["tool"], record["inputs"]), ("verify_artifact", {"path": str(self.file)}))

    def test_a_call_without_input_always_returns_the_instructions(self):
        self.runtime.execute_tool(USE_TOOL, {"name": "verify_artifact"}, 1)
        self.assertIn("Input schema", self.runtime.execute_tool(USE_TOOL, {"name": "verify_artifact"}, 1))

    def test_a_new_turn_shows_the_instructions_again(self):
        self.runtime.execute_tool(USE_TOOL, {"name": "verify_artifact"}, 1)
        self.runtime._episode_reads.pop(1, None)                     # what run_turn does when a turn starts
        shown = self.runtime.execute_tool(USE_TOOL, {"name": "verify_artifact", "input": {"path": str(self.file)}}, 1)
        self.assertIn("NOT run", shown)

    def test_one_chat_reading_the_instructions_does_not_open_the_tool_for_another(self):
        self.runtime.execute_tool(USE_TOOL, {"name": "verify_artifact"}, 1)
        self.assertIn("NOT run", self.runtime.execute_tool(USE_TOOL, {"name": "verify_artifact", "input": {"path": str(self.file)}}, 2))

    def test_a_name_that_is_not_listed_is_refused(self):
        for name in ("read_file", "nothing_like_this", None):
            self.assertIsInstance(self.runtime.execute_tool(USE_TOOL, {"name": name, "input": {}}, 1), Refused)

    def test_a_tool_outside_the_scope_stays_outside_it(self):
        with self.runtime.tool_scope({"read_file"}):
            self.assertIsInstance(self.runtime.execute_tool(USE_TOOL, {"name": "verify_artifact"}, 1), Refused)

    def test_each_showing_of_instructions_is_counted(self):
        seen = []
        self.runtime._inbox.record_event = lambda kind, **kw: seen.append((kind, kw.get("model")))
        self.runtime.execute_tool(USE_TOOL, {"name": "verify_artifact"}, 1)
        self.assertIn(("tool_instructions", "verify_artifact"), seen)

    def test_a_named_tool_called_directly_still_runs(self):
        """The owner's yes runs a parked call by its own name (core/parked_actions)."""
        self.assertIn("sha256", str(self.runtime.execute_tool("verify_artifact", {"path": str(self.file)}, 1)))

    def test_the_action_record_carries_the_id_of_the_use_tool_block(self):
        inputs = {"path": str(self.file)}
        self.runtime.execute_tool(USE_TOOL, {"name": "verify_artifact"}, 1)
        self.runtime.conversations[1] = [{"role": "assistant", "content": [
            {"type": "tool_use", "id": "call_7", "name": USE_TOOL, "input": {"name": "verify_artifact", "input": inputs}}]}]
        self.runtime.execute_tool(USE_TOOL, {"name": "verify_artifact", "input": inputs}, 1)
        self.assertEqual(self.records()[-1]["tool_use_id"], "call_7")


class SavedCalls(unittest.TestCase):
    def test_called_reads_both_forms(self):
        self.assertEqual(called({"name": "read_file", "input": {"path": "a"}}), ("read_file", {"path": "a"}))
        self.assertEqual(called({"name": USE_TOOL, "input": {"name": "desktop", "input": {"action": "take"}}}), ("desktop", {"action": "take"}))
        self.assertEqual(called({"name": USE_TOOL, "input": {"name": "desktop"}}), (USE_TOOL, {"name": "desktop"}))

    def test_a_protected_tool_keeps_its_result_when_called_through_use_tool(self):
        """consult_advisor's answer is worked from for the whole task; context pressure must not cut it."""
        long = "advice " * 2000
        messages = [{"role": "user", "content": "brief"}, {"role": "assistant", "content": "ok"}]
        messages += [{"role": "assistant", "content": [{"type": "tool_use", "id": "a1", "name": USE_TOOL,
                                                        "input": {"name": "consult_advisor", "input": {"question": "q"}}},
                                                       {"type": "tool_use", "id": "a2", "name": "read_file", "input": {"path": "x"}}]},
                     {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a1", "content": long},
                                                  {"type": "tool_result", "tool_use_id": "a2", "content": long}]}]
        messages += [{"role": "assistant", "content": "next"}, {"role": "user", "content": "more"}] * 4
        with tempfile.TemporaryDirectory() as tmp:
            out = context_window.prepare_context(messages, Path(tmp), budget_bytes=20_000)
        results = {b["tool_use_id"]: b["content"] for m in out if isinstance(m.get("content"), list)
                   for b in m["content"] if b.get("type") == "tool_result"}
        self.assertEqual(results["a1"], long)
        self.assertNotEqual(results["a2"], long)


if __name__ == "__main__":
    unittest.main()
