"""The Gemini API route: the model through Google's generateContent API with a key, no harness (2026-11-15).

The Gemini subscription reaches the model only through the Antigravity CLI, a coding-agent harness that wraps the
agent's prompt in its own instructions and hands tools over lazily. Alex: "Is there any way to remove the influence
of Gemini?" This route is that way: the agent's system prompt is the whole system instruction, the tools are declared
natively, history is sent as real turns, and the turn runs in the one turn loop the API routes share
(tabularium.core.turn_loop), so this provider only translates.

What it translates:
- the canonical history (Anthropic block format: text, image, tool_use, tool_result) into Gemini contents;
- the tool schemas into function declarations, keeping only the schema keys the API accepts;
- the reply envelope into responseJsonSchema, so the final answer is the declared object;
- Gemini 3+'s thought signatures: a function call's signature must travel back with it within the turn, so the
  provider remembers the signatures of the calls it made this turn and replays them (earlier turns are not checked);
- usage into the runtime's counters, with cachedContentTokenCount as cache_read (implicit caching; no cache writes).

The dependency is `requests`, already present; the Google SDK is not installed and not needed for one endpoint.
"""
import json
import time
import uuid

import requests

from tabularium.core import reply_envelope
from tabularium.core.turn_loop import Exchange

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MAX_OUTPUT_TOKENS = 16000
SCHEMA_KEYS = ("type", "description", "properties", "required", "enum", "items", "nullable", "format",
               "minimum", "maximum", "minItems", "maxItems", "anyOf")


class GeminiAPIError(RuntimeError):
    """An HTTP error from the API; status_code lets provider_routing.failure map 401, 429 and 5xx."""

    def __init__(self, status_code, text):
        super().__init__(f"Gemini API {status_code}: {text[:600]}")
        self.status_code = status_code


def clean_schema(value):
    """A JSON schema reduced to what Gemini's function declarations accept."""
    if isinstance(value, dict):
        out = {}
        for key, inner in value.items():
            if key not in SCHEMA_KEYS:
                continue
            if key in ("properties",):
                out[key] = {name: clean_schema(spec) for name, spec in inner.items()}
            elif key in ("items",):
                out[key] = clean_schema(inner)
            elif key == "anyOf":
                out[key] = [clean_schema(x) for x in inner]
            elif key == "format" and value.get("type") not in ("string", "number", "integer"):
                continue
            else:
                out[key] = inner
        if out.get("type") == "object" and "properties" not in out:
            out["properties"] = {}
        return out
    return value


def declarations(tools):
    """Anthropic-shaped tool schemas (name, description, input_schema) as Gemini function declarations."""
    return [{"name": t["name"], "description": t.get("description", ""),
             "parameters": clean_schema(t.get("input_schema") or {"type": "object", "properties": {}})}
            for t in tools if "input_schema" in t]


def _attr(block, name):
    return block.get(name) if isinstance(block, dict) else getattr(block, name, None)


def _inline(block):
    src = _attr(block, "source") or {}
    if src.get("type") == "base64" and src.get("data"):
        return {"inlineData": {"mimeType": src.get("media_type", "image/png"), "data": src["data"]}}
    return {"text": "[image unavailable]"}


def contents(history, signatures=None):
    """Canonical history as Gemini contents. Tool results need the call's name, found in the preceding assistant
    message; a call made this turn carries its thought signature back (signatures: id -> signature)."""
    signatures = signatures or {}
    names = {}
    out = []
    for message in history:
        role, content = message["role"], message["content"]
        if isinstance(content, str):
            if content.strip():
                out.append({"role": "model" if role == "assistant" else "user", "parts": [{"text": content}]})
            continue
        parts = []
        if role == "assistant":
            for block in content:
                kind = _attr(block, "type")
                if kind == "text" and (_attr(block, "text") or "").strip():
                    parts.append({"text": _attr(block, "text")})
                elif kind == "tool_use":
                    ident = _attr(block, "id")
                    names[ident] = _attr(block, "name")
                    part = {"functionCall": {"name": _attr(block, "name"), "args": _attr(block, "input") or {}}}
                    if ident in signatures:
                        part["thoughtSignature"] = signatures[ident]
                    parts.append(part)
            if parts:
                out.append({"role": "model", "parts": parts})
            continue
        images = []
        for block in content:
            kind = _attr(block, "type")
            if kind == "text":
                parts.append({"text": _attr(block, "text") or ""})
            elif kind == "image":
                parts.append(_inline(block))
            elif kind == "tool_result":
                inner = _attr(block, "content")
                if isinstance(inner, list):
                    texts = []
                    for x in inner:
                        if _attr(x, "type") == "text":
                            texts.append(_attr(x, "text") or "")
                        elif _attr(x, "type") == "image":
                            images.append(_inline(x))
                    inner = "\n".join(texts) or "(see image)"
                ident = _attr(block, "tool_use_id")
                response = {"output": str(inner)}
                if _attr(block, "is_error"):
                    response["error"] = True
                parts.append({"functionResponse": {"name": names.get(ident, "tool"), "response": response}})
        parts.extend(images)
        if parts:
            out.append({"role": "user", "parts": parts})
    return out


def usage_counters(meta):
    """usageMetadata as the runtime's counters. Thinking tokens are output; cached prompt tokens are cache reads."""
    if not isinstance(meta, dict):
        return None
    prompt = meta.get("promptTokenCount")
    cached = meta.get("cachedContentTokenCount") or 0
    output = (meta.get("candidatesTokenCount") or 0) + (meta.get("thoughtsTokenCount") or 0)
    return dict(input_total=prompt, cache_read=cached if prompt is not None else None, cache_write=0,
                input_uncached=(prompt - cached) if prompt is not None else None, output=output)


class GeminiGenerate:
    """One Exchange per call to generateContent, for tabularium.core.turn_loop.run."""

    def __init__(self, agent, chat_id, post=None):
        self.agent, self.chat_id = agent, chat_id
        self.post = post or requests.post
        self.model = agent._routing.model("gemini") or agent.GEMINI_MODEL_DEFAULT
        self.tools = declarations(agent.get_tools("anthropic"))
        route = agent._routing.label(agent._routing.ACTIVE.get()) if agent._routing.ACTIVE.get() else f"{self.model} via Gemini API"
        self.system = agent.SYSTEM_PROMPT + (
            f"\n\n[Runtime model: {route} If Alex asks which model is answering, state the active model clearly.]")
        self.signatures = {}                                     # call id -> thought signature, this turn only
        self.schema_in_request = True                            # dropped once if the model rejects schema with tools

    def request(self, api_messages):
        body = {
            "systemInstruction": {"parts": [{"text": self.system}]},
            "contents": contents(api_messages, self.signatures),
            "tools": [{"functionDeclarations": self.tools}],
            "generationConfig": {"maxOutputTokens": MAX_OUTPUT_TOKENS},
        }
        if self.schema_in_request:
            body["generationConfig"]["responseMimeType"] = "application/json"
            body["generationConfig"]["responseJsonSchema"] = reply_envelope.SCHEMA
        return body

    def call(self, body):
        response = self.post(ENDPOINT.format(model=self.model), json=body, timeout=(30, 600),
                             headers={"x-goog-api-key": self.agent.GEMINI_API_KEY, "Content-Type": "application/json"})
        if response.status_code != 200:
            raise GeminiAPIError(response.status_code, response.text)
        return response.json()

    def exchange(self, api_messages):
        agent = self.agent
        if not agent.GEMINI_API_KEY:
            raise agent._routing.Unavailable("No Gemini API key configured")
        context_retries = 0
        payload = None
        for attempt in range(6):
            try:
                agent._inbox.record_event("attempt", self.model, "api", "gemini")
                payload = self.call(self.request(api_messages))
                break
            except GeminiAPIError as exc:
                text = str(exc).lower()
                if self.schema_in_request and exc.status_code == 400 and "mime" in text and "unsupported" in text:
                    # This model refuses a response schema beside tools (the 2.5 generation did). The envelope is
                    # then asked for in words and parsed as JSON text, like the Claude subscription route's result.
                    self.schema_in_request = False
                    self.system += ("\n\n[Answer with one JSON object matching this schema and nothing else: "
                                    + json.dumps(reply_envelope.SCHEMA, ensure_ascii=False) + "]")
                    print(f"[gemini] {self.model} does not take a response schema beside tools; asking for the envelope in words.")
                    continue
                if exc.status_code == 400 and agent._is_context_window_error(exc) and context_retries < 2:
                    api_messages = agent._compact_context_retry(api_messages, self.chat_id)
                    context_retries += 1
                    print(f"[context] Gemini request exceeded the model window; compacted history to {len(api_messages)} messages and retrying.")
                    continue
                if exc.status_code in (429, 500, 502, 503) and attempt < 2:
                    wait = 10 * (2 ** attempt)
                    print(f"Gemini {exc.status_code}, retrying in {wait}s (attempt {attempt + 1}/3)...")
                    time.sleep(wait)
                    continue
                raise
        if payload is None:
            raise RuntimeError("Model request could not complete within the bounded retry budget; history is preserved.")

        usage = usage_counters(payload.get("usageMetadata"))
        agent._inbox.record_event("response", self.model, "api", "gemini", usage)
        if usage:
            print(f"[usage] api {self.model} provider-reported {json.dumps(usage)}")
        candidates = payload.get("candidates") or []
        if not candidates:
            reason = (payload.get("promptFeedback") or {}).get("blockReason", "")
            return Exchange(text="", stop=f"no candidate{': ' + reason if reason else ''}", history=api_messages)
        candidate = candidates[0]
        finish = candidate.get("finishReason", "")
        text, blocks, calls = "", [], []
        for part in (candidate.get("content") or {}).get("parts") or []:
            if part.get("thought"):
                continue
            if "functionCall" in part:
                call = part["functionCall"]
                ident = "gem_" + uuid.uuid4().hex[:24]
                if part.get("thoughtSignature"):
                    self.signatures[ident] = part["thoughtSignature"]
                args = call.get("args") or {}
                blocks.append({"type": "tool_use", "id": ident, "name": call.get("name", ""), "input": args})
                calls.append((ident, call.get("name", ""), args))
            elif "text" in part:
                text += part["text"]
        if text.strip():
            blocks.insert(0, {"type": "text", "text": reply_envelope.readable(text)})
        truncated = finish == "MAX_TOKENS"
        stop = "" if finish in ("STOP", "MAX_TOKENS", "") or calls else finish.lower()
        return Exchange(text=text, blocks=blocks, calls=calls, truncated=truncated, stop=stop, history=api_messages)
