"""One real turn per agent, end to end, with a scripted model (2026-10-11).

Loads each agent's telegram.py in isolation (as a background worker, so it does not claim the live agent's port),
replaces only the model call, and drives one turn: the scripted model asks for read_file on the identity file, the real
tool runs, the model answers OK. This exercises the tool schema, the dispatcher, the tool, the history handling and
the action receipts. It would have caught Tacitus's missing-name failure of 7-9 October and the tool-name mismatch."""
import json, sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace as NS
ROOT = Path(__file__).resolve().parents[1]
from agent_harness import AGENTS, load_agent


class Turn(unittest.TestCase):
    def test_each_agent_completes_a_turn_with_a_tool_call(self):
        for agent in AGENTS:
            with self.subTest(agent=agent), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / agent
                root.mkdir()
                scope = load_agent(agent, root)
                runtime = scope["runtime"]
                runtime._background.checkpoint(1,1,'','',[],guesses=[],completion_items=[
                    {'item':'Read identity and answer','check':'Read response'}])
                runtime.OPENAI_API_KEY = "test"
                token = runtime._routing.ACTIVE.set({"kind": "openai_api", "model": "gpt-test"})
                calls = []

                def fake(client, *, model, instructions, input_items, tools, plan_mode):
                    calls.append(len(input_items))
                    names = {t.get("name") for t in tools}
                    self.assertIn("read_file", names, agent)
                    if len(calls) == 1:
                        item = NS(type="function_call", name="read_file", call_id="c1",
                                  arguments=json.dumps({"path": str(root / "SKILL.md")}))
                    else:
                        item = NS(type="message", content=[NS(type="output_text", text='{"reply": "OK", "task_outcome": "completed"}')])
                    return NS(output=[item], status="completed", incomplete_details=None, usage=None), "", [], []
                runtime._responses_call = fake
                try:
                    messages = [{"role": "user", "content": "Self-test: read the identity file and reply OK."}]
                    reply = runtime.run_turn_openai(messages, 1)
                finally:
                    runtime._routing.ACTIVE.reset(token)
                self.assertEqual(reply, "OK", (agent, reply))
                self.assertEqual(len(calls), 2, agent)                       # tool call, then the answer
                tool_results = [b for m in messages if isinstance(m.get("content"), list) for b in m["content"]
                                if isinstance(b, dict) and b.get("type") == "tool_result"]
                self.assertEqual(len(tool_results), 1, agent)
                body = tool_results[0]["content"]
                body = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
                self.assertIn("Test identity", body, (agent, body[:200]))     # the real read_file ran on the real file
                receipts = list((root / "task_state" / "background" / "1" / "task_state" / "actions").glob("*.json"))
                self.assertTrue(receipts, agent)                              # and left an action receipt


if __name__ == "__main__":
    unittest.main()
