"""Token savings must preserve evidence, call identity, error status, and current instructions."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from agent_harness import make_agent
from agent_core.responses_context import ResponsesContext
from agent_core.command_output import command_result
from agent_core.context_window import prepare_context
from agent_core.outcome import Failed
from agent_core.turn_loop import OpenAIResponses, run


class Efficiency(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_native_items_survive_real_turn_loop_exactly(self):
        agent = make_agent("callimachus", self.root)
        agent.OPENAI_API_KEY = "test"
        token = agent._routing.ACTIVE.set({"kind": "openai_api", "model": "gpt-test"})
        self.addCleanup(agent._routing.ACTIVE.reset, token)
        requests = []
        raw_args = '{ "path" : ' + json.dumps(str(self.root / "SKILL.md")) + ' }'
        native = [{"type": "reasoning", "id": "reason1", "summary": [], "encrypted_content": "opaque-reasoning"},
                  {"type": "function_call", "id": "fc1", "call_id": "call1", "name": "read_file", "arguments": raw_args}]
        def model(client, **kw):
            requests.append(deepcopy(kw["input_items"]))
            output = [NS(**x) for x in native] if len(requests) == 1 else [
                NS(type="message", content=[NS(type="output_text", text='{"reply":"Done"}')])]
            return NS(output=output, status="completed", incomplete_details=None, usage=None), "", [], []
        agent._responses_call = model
        messages = [{"role": "user", "content": "Read the identity file"}]
        self.assertEqual(run(agent, messages, 1, OpenAIResponses(agent, 1)), "Done")
        self.assertEqual(requests[1][:len(requests[0])], requests[0])
        self.assertEqual(requests[1][len(requests[0]):len(requests[0])+2], native)
        self.assertEqual(requests[1][-1]["call_id"], "call1")
        self.assertNotIn("opaque-reasoning", json.dumps(messages))  # native state never leaks into portable history

    def test_context_cut_or_model_switch_discards_native_continuation(self):
        state = ResponsesContext()
        history = [{"role": "user", "content": "original"}]
        blocks = [{"type": "tool_use", "id": "x", "name": "read_file", "input": {}}]
        state.remember(history, blocks, history, [{"type": "reasoning", "encrypted_content": "opaque"}], "a")
        changed = [{"role": "user", "content": "corrected instruction"}]
        self.assertEqual(state.prepare(changed, deepcopy, "a"), changed)
        complete = history + [{"role": "assistant", "content": blocks}]
        self.assertEqual(state.prepare(complete, deepcopy, "b"), complete)

    def test_old_outputs_do_not_change_a_below_limit_prefix(self):
        history = [{"role": "user", "content": "brief"}, {"role": "assistant", "content": "ready"}]
        for i in range(7):
            previous = deepcopy(history)
            history += [{"role": "assistant", "content": [{"type": "tool_use", "id": str(i), "name": "read_file", "input": {}}]},
                        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": str(i), "content": "abc " * 1500}]}]
            prepared = prepare_context(history, self.root)
            self.assertEqual(prepared[:len(previous)], previous)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_middle_failure_and_complete_artifact_survive_verbose_output(self):
        output = "passed fixture\n" * 500 + "ERROR: balance mismatch expected 73 got 71\n" + "passed fixture\n" * 500
        result = command_result(output, "WARNING: partial data\n", 1, self.root)
        self.assertIsInstance(result, Failed)
        data = json.loads(result)
        self.assertFalse(data["passed"])
        self.assertIn("expected 73 got 71", data["excerpt"])
        self.assertIn("WARNING: partial data", data["excerpt"])
        self.assertEqual(Path(data["archive"]).read_text(encoding="utf-8"), "STDOUT:\n" + output + "\nSTDERR:\nWARNING: partial data\n")
        self.assertLess(len(result), len(output) // 3)

    def test_web_content_removes_code_not_visible_evidence_and_retrieves_middle(self):
        from agent_core.web_content import visible_text, page_result
        html = b"<html><head><script>tracking_payload = 999999;</script><style>.x{color:red}</style></head><body>"
        html += b"<p>Unrelated visible text.</p>" * 350
        html += b"<table><tr><td>Revenue</td><td>123.4 million USD</td></tr></table>"
        html += b"<p>Unrelated visible text.</p>" * 350 + b"<p>Unaudited; preliminary figures.</p></body></html>"
        text = visible_text(html)
        self.assertNotIn("tracking_payload", text)
        self.assertIn("123.4 million USD", text)
        result = page_result(text, "https://example.test/report", self.root, "Revenue")
        self.assertIn("123.4 million USD", result)
        self.assertIn("Unaudited", result)
        self.assertIn("https://example.test/report", result)
        self.assertLess(len(result), len(text) / 2)
        archives = list(self.root.glob("*.txt"))
        self.assertEqual(archives[0].read_text(encoding="utf-8"), text)

    def test_worker_can_retrieve_its_log_and_legacy_agent_archive(self):
        agent = make_agent("callimachus", self.root, background=True)
        for directory in [agent.RUNTIME_DIR / "context_archive" / "results", agent.AGENT_DIR / "context_archive" / "results"]:
            result = json.loads(command_result("noise\n" * 1000 + "unique_failure expected 73\n", "", 1, directory))
            found = agent._execute_tool_unguarded("search_archived", {"query":"unique_failure", "path":result["archive"]}, 1)
            self.assertIn("unique_failure", found)

    def test_exit_code_and_timeout_are_authoritative_not_log_words(self):
        self.assertIsInstance(command_result("All checks passed", "", 3, self.root), Failed)
        self.assertIsInstance(command_result("OK", "", 0, self.root, timed_out=True), Failed)
        self.assertNotIsInstance(command_result("expected error test passed", "", 0, self.root), Failed)


if __name__ == "__main__":
    unittest.main()
