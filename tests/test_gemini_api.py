"""The Gemini API route (tabularium.models.gemini_api): the model through Google's API with a key, no harness
(2026-11-15, Alex: "Build the Gemini API route"). A provider for the shared turn loop: it translates history, tools,
the envelope and usage, replays this turn's thought signatures, and maps HTTP failures so routing can fall through."""
import json, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

from tabularium.core import reply_envelope
from tabularium.core.agent_runtime import DurableInbox
from tabularium.models import gemini_api, provider_routing as routing
from tabularium.models.usage import normalize

IMAGE = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "abc="}}
ENVELOPE = {"reply": "All done", "modality": "text", "work": "none", "task_outcome": "none", "question": "",
            "decision_task_id": 0, "decision_id": 0, "status_claims": [], "correction": [], "not_correction": [],
            "answers": [], "request": "done"}


def response(status=200, **payload):
    return NS(status_code=status, text=json.dumps(payload), json=lambda: payload)


class Translation(unittest.TestCase):
    def test_tool_schemas_keep_only_what_the_api_accepts(self):
        tools = [{"name": "read_memory", "description": "Read core memory",
                  "input_schema": {"type": "object", "additionalProperties": False, "$schema": "x",
                                   "properties": {"section": {"type": "string", "enum": ["core"], "default": "core",
                                                              "description": "which"}},
                                   "required": ["section"]}},
                 {"name": "no_schema"}]
        self.assertEqual(gemini_api.declarations(tools), [{
            "name": "read_memory", "description": "Read core memory",
            "parameters": {"type": "object", "properties": {"section": {"type": "string", "enum": ["core"], "description": "which"}},
                           "required": ["section"]}}])

    def test_history_becomes_contents_with_names_images_and_this_turns_signatures(self):
        history = [
            {"role": "user", "content": "Check memory"},
            {"role": "assistant", "content": [{"type": "text", "text": "Looking."},
                                              {"type": "tool_use", "id": "gem_1", "name": "read_memory", "input": {"section": "core"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "gem_1",
                                          "content": [{"type": "text", "text": "goals"}, IMAGE]}]},
            {"role": "user", "content": [{"type": "text", "text": "and this"}, IMAGE]},
            {"role": "assistant", "content": ""},
        ]
        out = gemini_api.contents(history, {"gem_1": "sig-A"})
        self.assertEqual(out[0], {"role": "user", "parts": [{"text": "Check memory"}]})
        self.assertEqual(out[1], {"role": "model", "parts": [
            {"text": "Looking."},
            {"functionCall": {"name": "read_memory", "args": {"section": "core"}}, "thoughtSignature": "sig-A"}]})
        self.assertEqual(out[2], {"role": "user", "parts": [
            {"functionResponse": {"name": "read_memory", "response": {"output": "goals"}}},
            {"inlineData": {"mimeType": "image/jpeg", "data": "abc="}}]})
        self.assertEqual(out[3]["parts"][1], {"inlineData": {"mimeType": "image/jpeg", "data": "abc="}})
        self.assertEqual(len(out), 4)                                  # the empty assistant message is not sent
        self.assertNotIn("thoughtSignature", json.dumps(gemini_api.contents(history)))   # earlier turns: no signature

    def test_usage_counts_cached_prompt_as_cache_read_and_thinking_as_output(self):
        counters = gemini_api.usage_counters({"promptTokenCount": 1200, "cachedContentTokenCount": 1000,
                                              "candidatesTokenCount": 40, "thoughtsTokenCount": 15})
        self.assertEqual(counters, dict(input_total=1200, cache_read=1000, cache_write=0, input_uncached=200, output=55))
        self.assertEqual(normalize("gemini", counters), counters)
        self.assertIsNone(gemini_api.usage_counters(None))


class Provider(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        folder = Path(self.tmp.name)
        self.agent = NS(
            GEMINI_API_KEY="k", GEMINI_MODEL_DEFAULT="gemini-4-carbon-medium", SYSTEM_PROMPT="Own persona",
            get_tools=lambda _: [{"name": "read_memory", "description": "Read core memory",
                                  "input_schema": {"type": "object", "properties": {"section": {"type": "string"}}}}],
            _routing=routing, _inbox=DurableInbox(folder, lambda *a: None),
            _is_context_window_error=lambda exc: "token" in str(exc).lower() and "exceed" in str(exc).lower(),
            _compact_context_retry=Mock(side_effect=lambda m, c: m[-1:]),
        )
        self.token = routing.ACTIVE.set({"kind": "gemini_api", "model": "gemini-4-carbon-medium"})
        self.addCleanup(routing.ACTIVE.reset, self.token)

    def test_a_function_call_becomes_a_tool_call_with_its_signature_remembered(self):
        posts = []
        def post(url, json=None, timeout=None, headers=None):
            posts.append((url, json, headers))
            return response(candidates=[{"finishReason": "STOP", "content": {"parts": [
                {"thought": True, "text": "hidden"},
                {"functionCall": {"name": "read_memory", "args": {"section": "goals"}}, "thoughtSignature": "sig-1"}]}}],
                usageMetadata={"promptTokenCount": 10, "candidatesTokenCount": 5})
        provider = gemini_api.GeminiGenerate(self.agent, 1, post=post)
        exchange = provider.exchange([{"role": "user", "content": "Check memory"}])
        url, body, headers = posts[0]
        self.assertIn("gemini-4-carbon-medium:generateContent", url)
        self.assertEqual(headers["x-goog-api-key"], "k")
        self.assertEqual(body["systemInstruction"]["parts"][0]["text"][:11], "Own persona")
        self.assertEqual(body["tools"][0]["functionDeclarations"][0]["name"], "read_memory")
        self.assertEqual(body["generationConfig"]["responseJsonSchema"], reply_envelope.SCHEMA)
        self.assertEqual(len(exchange.calls), 1)
        ident, name, args = exchange.calls[0]
        self.assertEqual((name, args), ("read_memory", {"section": "goals"}))
        self.assertEqual(exchange.blocks, [{"type": "tool_use", "id": ident, "name": "read_memory", "input": {"section": "goals"}}])
        self.assertEqual(provider.signatures, {ident: "sig-1"})
        self.assertFalse(exchange.truncated); self.assertEqual(exchange.stop, "")
        # The next request replays the signature on that call, as Gemini 3+ requires within the turn.
        provider.exchange([{"role": "user", "content": "Check memory"},
                           {"role": "assistant", "content": exchange.blocks},
                           {"role": "user", "content": [{"type": "tool_result", "tool_use_id": ident, "content": "goals"}]}])
        self.assertEqual(posts[1][1]["contents"][1]["parts"][0]["thoughtSignature"], "sig-1")

    def test_the_final_answer_is_the_envelope_and_a_cut_off_is_reported(self):
        provider = gemini_api.GeminiGenerate(self.agent, 1, post=lambda *a, **k: response(
            candidates=[{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(ENVELOPE)}]}}]))
        exchange = provider.exchange([{"role": "user", "content": "Hi"}])
        self.assertEqual(reply_envelope.reply(exchange.text), "All done")
        self.assertEqual(exchange.blocks, [{"type": "text", "text": "All done"}])
        cut = gemini_api.GeminiGenerate(self.agent, 1, post=lambda *a, **k: response(
            candidates=[{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "{\"reply\": \"half"}]}}]))
        self.assertTrue(cut.exchange([{"role": "user", "content": "Hi"}]).truncated)
        blocked = gemini_api.GeminiGenerate(self.agent, 1, post=lambda *a, **k: response(
            candidates=[{"finishReason": "SAFETY", "content": {"parts": []}}]))
        self.assertEqual(blocked.exchange([{"role": "user", "content": "Hi"}]).stop, "safety")

    def test_a_model_that_refuses_schema_beside_tools_is_asked_in_words_once(self):
        calls = []
        def post(url, json=None, timeout=None, headers=None):
            calls.append(json)
            if "responseJsonSchema" in json["generationConfig"]:
                return response(400, error={"message": "Function calling with a response mime type: 'application/json' is unsupported"})
            return response(candidates=[{"finishReason": "STOP", "content": {"parts": [{"text": __import__("json").dumps(ENVELOPE)}]}}])
        provider = gemini_api.GeminiGenerate(self.agent, 1, post=post)
        exchange = provider.exchange([{"role": "user", "content": "Hi"}])
        self.assertEqual(reply_envelope.reply(exchange.text), "All done")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("responseJsonSchema", calls[1]["generationConfig"])
        self.assertIn("Answer with one JSON object", calls[1]["systemInstruction"]["parts"][0]["text"])

    def test_http_failures_carry_their_status_for_routing_and_a_full_window_is_compacted(self):
        provider = gemini_api.GeminiGenerate(self.agent, 1, post=lambda *a, **k: response(401, error={"message": "API key not valid"}))
        with self.assertRaises(gemini_api.GeminiAPIError) as ctx:
            provider.exchange([{"role": "user", "content": "Hi"}])
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIsInstance(routing.failure(ctx.exception), routing.Unavailable)   # routing falls through, no replay
        seen = []
        def post(url, json=None, timeout=None, headers=None):
            seen.append(len(json["contents"]))
            if len(json["contents"]) > 1:
                return response(400, error={"message": "The input token count exceeds the maximum number of tokens allowed"})
            return response(candidates=[{"finishReason": "STOP", "content": {"parts": [{"text": __import__("json").dumps(ENVELOPE)}]}}])
        provider = gemini_api.GeminiGenerate(self.agent, 1, post=post)
        provider.exchange([{"role": "user", "content": "one"}, {"role": "assistant", "content": "two"}, {"role": "user", "content": "three"}])
        self.assertEqual(seen, [3, 1])
        self.agent.GEMINI_API_KEY = ""
        with self.assertRaises(routing.Unavailable):
            gemini_api.GeminiGenerate(self.agent, 1, post=post).exchange([{"role": "user", "content": "Hi"}])


class Routing(unittest.TestCase):
    def test_the_route_is_a_kind_that_needs_the_key_and_runs_the_gemini_turn(self):
        self.assertEqual(routing.KINDS["gemini_api"], "Gemini API (paid)")
        self.assertEqual(routing.validate({"routes": [{"kind": "gemini_api", "model": "gemini-4-carbon-high"}]})["routes"],
                         [{"kind": "gemini_api", "model": "gemini-4-carbon-high"}])
        with self.assertRaises(ValueError):
            routing.validate({"routes": [{"kind": "gemini_api", "model": "gpt-6.1-sol"}]})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            routing.save(root, {"routes": [{"kind": "gemini_api", "model": "gemini-4-carbon-high"},
                                           {"kind": "openai_api", "model": "gpt-test"}]})
            agent = NS(AGENT_DIR=root, RUNTIME_DIR=root, _review_counts={}, _strip_old_images=Mock(), send_message=Mock(),
                       OPENAI_API_KEY="k", ANTHROPIC_API_KEY="", GEMINI_API_KEY="",
                       run_turn_gemini=Mock(return_value="Gemini"), run_turn_openai=Mock(return_value="OpenAI"))
            self.assertEqual(routing.run(agent, [], 1), "OpenAI")              # no key: the next route runs
            agent.run_turn_gemini.assert_not_called()
            agent.GEMINI_API_KEY = "k"
            routing.clear_override(root)
            (root / "task_state" / "route_gemini_api.json").unlink(missing_ok=True)
            self.assertEqual(routing.run(agent, [], 1), "Gemini")
            agent.run_turn_gemini.assert_called_once_with([], 1)
        token = routing.ACTIVE.set({"kind": "gemini_api", "model": "gemini-4-carbon-high"})
        try:
            self.assertEqual(routing.provider(), "gemini")
            self.assertEqual(routing.model("gemini"), "gemini-4-carbon-high")
        finally:
            routing.ACTIVE.reset(token)


if __name__ == "__main__":
    unittest.main()
