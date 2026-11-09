"""The daily self-test goes through the message handler's composition (2026-10-11).

On 2026-10-11 every real message failed with a NameError for sixteen minutes while the self-test kept passing, because
it called run_turn directly. Now run_selftest builds a synthetic Telegram message and composes it with compose_turn,
the method handle_message appends. This test loads Cato in isolation, replaces only run_turn, and checks that the turn
the self-test runs carries what the handler would add (timestamp tag, the instruction), that the activity nudge counter
is untouched, and that the record lands in task_state/selftest.json."""
import json, sys, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
from agent_harness import load_agent


class SelftestPath(unittest.TestCase):
    def test_selftest_composes_like_the_handler(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "coordinator"; root.mkdir()
            runtime = load_agent("coordinator", root)["runtime"]
            seen = []

            def fake_run_turn(messages, chat_id):
                seen.append(messages[-1]["content"])
                messages.append({"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "x"}}]})
                return "OK"
            runtime.run_turn = fake_run_turn
            runtime._turns_since_activity[1] = 3
            from tabularium.channels.owner_telegram import OWNER_CHAT_ID
            runtime._turns_since_activity[OWNER_CHAT_ID] = 3
            record = runtime.run_selftest()
            self.assertTrue(record["ok"], record)
            content = seen[0]
            self.assertIsInstance(content, str)
            self.assertRegex(content, r"^\[\d{4}-\d{2}-\d{2} \w+ \d{2}:\d{2}\]")          # the handler's timestamp tag
            self.assertIn("Runtime self-test", content)
            self.assertIn("reply with exactly the word OK", content)
            self.assertEqual(runtime._turns_since_activity[OWNER_CHAT_ID], 3)            # nudge counter untouched
            saved = json.loads((root / "task_state" / "selftest.json").read_text(encoding="utf-8"))
            self.assertTrue(saved["ok"])

    def test_selftest_reports_a_handler_composition_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "coordinator"; root.mkdir()
            runtime = load_agent("coordinator", root)["runtime"]
            def broken(message, nudge=True):
                raise NameError("name '_language_hint' is not defined")
            runtime.compose_turn = broken
            record = runtime.run_selftest()
            self.assertFalse(record["ok"])
            self.assertIn("NameError", record["error"])


if __name__ == "__main__":
    unittest.main()
