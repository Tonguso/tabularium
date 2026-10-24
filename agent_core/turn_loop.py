"""One turn loop for the API routes; a provider only translates (2026-10-15).

Until today run_turn_openai and run_turn_anthropic each carried the whole turn: the background boundary, the abort
check, context preparation, the 16k cut-off handling, tool execution, the budget warning, the step limit, the reply
envelope and every save. Every fix this week was made twice, and the reasoning leak took three fixes because there were
three places to leak from. Now `run` owns the turn and asks a provider for one Exchange at a time; OpenAIResponses and
AnthropicMessages hold only what differs: the client, the request, the retry policy and the shape of the response.

The Claude subscription route (subscription_runtime.run_claude) is not a provider here: the Claude Code SDK runs the
tool loop inside its own process and hands tools back through MCP, so our loop never sees its steps.
"""
from dataclasses import dataclass, field
import json
import time

from agent_core import reply_envelope
from agent_core.responses_context import ResponsesContext
from agent_core.session_lifecycle import SessionLimit
from agent_core.reply_envelope import Reply


@dataclass
class Exchange:
    """One model response in route-neutral form."""
    text: str = ""                                   # the final text: a reply envelope, or empty
    blocks: list = field(default_factory=list)       # assistant content to persist (text and tool_use dicts)
    calls: list = field(default_factory=list)        # (id, name, input) for each tool call
    truncated: bool = False                          # output cut at the token limit
    stop: str = ""                                   # the provider's stop label when it is neither an answer nor a tool call
    history: list = None                             # the request history after any compaction the provider had to do


CUT_OFF_NOTE = ("[System: your response was CUT OFF at the 16,000-token output limit before the tool call finished. "
                "NOTHING was written to disk. Do not repeat the same whole-file write. Continue with smaller steps: use "
                "edit_file for targeted changes to an existing file, or write a new file in sections (write_file, then "
                "edit_file to append). Keep each tool call well under the limit.]")
CUT_OFF_FINAL = ("\n\n[Cut off: my output hit the 16k-token limit three times on this task, so nothing was written. "
                 "I'll need to do it in smaller pieces — tell me to continue and I'll use edit_file step by step.]")



def budget_warning(step, limit):
    return (f"[System: {step} of {limit} tool rounds used in this session. Save task_checkpoint with evidence and next action. "
            "The runtime continues the task from saved results at the session boundary; do not claim unfinished work is complete.]")


def run(agent, messages, chat_id, provider):
    """The turn. `messages` is the saved conversation; the provider sees a trimmed, sanitised, prepared copy."""
    agent._truncations.pop(chat_id, None)
    turn_started = time.time()                     # an accepted start/resume/reopen after this backs "work started"
    reminded = False
    covered_asked = False
    unblocked_asked = False
    # Trim FIRST, on the raw in-memory list whose dict objects are stable (that is what the window anchor keys on),
    # then sanitise only the window that is actually sent.
    api_messages = agent._sanitise_tool_history(agent.trim_history(messages, chat_id))

    def save(message):
        messages.append(message)
        agent.save_conversation(chat_id)
        api_messages.append(message)

    def end(text, complete=True):
        messages.append({"role": "assistant", "content": text})
        # The shared delivery boundary verifies the draft before persisting it as a reply.
        return text if isinstance(text, Reply) else Reply(text, complete=complete)

    for step in range(agent.MAX_TOOL_ITERATIONS):
        agent._background_boundary(messages, api_messages, chat_id)
        agent._inbox.progress("model", deadline_seconds=2100)
        if agent._abort_requested.pop(chat_id, False):
            return end("Stopped.", complete=False)

        api_messages = agent._prepare_request_context(api_messages, chat_id)
        exchange = provider.exchange(api_messages)
        if isinstance(exchange, str):                      # the provider ended the turn with a message of its own
            return end(exchange, complete=False)
        if exchange.history is not None:
            api_messages = exchange.history
        agent._check_task_waiting(chat_id)

        if exchange.truncated:
            # Output cap hit mid-generation, usually inside a huge write_file. The partial tool call cannot be
            # executed; keep the text, tell the model what happened, and let it continue with smaller steps.
            # Twice at most, then report honestly.
            truncations = agent._truncations.get(chat_id, 0) + 1
            agent._truncations[chat_id] = truncations
            print(f"[max_tokens] output cut at 16k (#{truncations} this turn); dropped {len(exchange.calls)} partial call(s)")
            try:
                kept = reply_envelope.reply(exchange.text)
            except reply_envelope.ReplyMissing:
                kept = "(response cut off before any text)"
            save({"role": "assistant", "content": kept})
            if truncations > 2:
                agent._truncations.pop(chat_id, None)
                agent._inbox.record_event("bounce", "cut_off", role="runtime")
                messages.pop()
                return end(kept + CUT_OFF_FINAL, complete=False)
            save({"role": "user", "content": [{"type": "text", "text": CUT_OFF_NOTE}]})
            continue

        if exchange.stop:
            return end(exchange.text or f"(stopped: {exchange.stop})", complete=False)

        if not exchange.calls:
            final = reply_envelope.reply(exchange.text)        # only the declared reply is delivered and saved
            if not agent.BACKGROUND_TASK_ID and not reminded and                     reply_envelope.unbacked(final, agent._background.activated_since(chat_id, turn_started)):
                reminded = True                                 # once: the model starts the work or asks for a go
                agent._inbox.record_event("bounce", "unbacked_promise", role="runtime")
                save({"role": "user", "content": reply_envelope.UNBACKED_PROMISE})
                continue
            if not agent.BACKGROUND_TASK_ID and not unblocked_asked and reply_envelope.dead_end(final):
                unblocked_asked = True                          # once: another route, a question, or an honest stop
                agent._inbox.record_event("bounce", "dead_end", role="runtime")
                save({"role": "user", "content": reply_envelope.DEAD_END})
                continue
            missing = [] if agent.BACKGROUND_TASK_ID or covered_asked else reply_envelope.unanswered(
                final, agent._inbox.turn_messages() if hasattr(agent._inbox, "turn_messages") else [])
            if missing:
                covered_asked = True                            # once: answer them or say how each is handled
                agent._inbox.record_event("bounce", "unanswered_message", role="runtime")
                save({"role": "user", "content": reply_envelope.unanswered_note(missing)})
                continue
            return end(final)

        save({"role": "assistant", "content": exchange.blocks})
        results = [{'type': 'tool_result', 'tool_use_id': ident,
                    'content': 'Not executed: the task stopped before this action.', 'is_error': True}
                   for ident, name, inputs in exchange.calls]
        save({'role': 'user', 'content': results})
        for result, (ident, name, inputs) in zip(results, exchange.calls):
            agent._check_task_waiting(chat_id)
            result['content'] = 'Outcome unknown: tool started; inspect action receipts before retrying.'
            agent.save_conversation(chat_id)
            result['content'] = agent.execute_tool(name, inputs, chat_id)
            result.pop('is_error', None)
            agent.save_conversation(chat_id)
        if step + 1 == agent.TOOL_BUDGET_WARN_AT:
            # Once. Without it a long task simply hits the wall and Alex gets nothing after ten minutes.
            results.append({"type": "text", "text": budget_warning(step + 1, agent.MAX_TOOL_ITERATIONS)})
        agent.save_conversation(chat_id)

    agent._inbox.record_event("bounce", "step_limit", role="runtime")
    agent.save_conversation(chat_id)
    raise SessionLimit("API tool-round session limit")


class AnthropicMessages:
    """Anthropic Messages API, streamed, with overload fallback across OVERLOAD_FALLBACK_MODELS."""

    def __init__(self, agent, chat_id):
        self.agent, self.chat_id = agent, chat_id
        self.tools = agent.get_tools("anthropic")
        self.model = agent.get_anthropic_model(chat_id)

    def exchange(self, api_messages):
        import anthropic
        agent = self.agent
        response = None
        context_retries = 0
        candidates = [self.model] if agent._routing.ACTIVE.get() else \
            [self.model] + [m for m in agent.OVERLOAD_FALLBACK_MODELS if m != self.model]
        last_err = None
        for cand in candidates:
            overload_retries = 0
            for attempt in range(5):  # initial request + two context and two overload retries
                try:
                    # Streamed: a long generation keeps the connection alive, so it cannot trip the client timeout.
                    agent._inbox.record_event("attempt", cand, "api", "anthropic")
                    with agent.anthropic_client.messages.stream(
                        model=cand,
                        max_tokens=16000,
                        system=[{"type": "text", "text": agent.SYSTEM_PROMPT, "cache_control": dict(agent.CACHE_CONTROL)}],
                        tools=self.tools,
                        messages=agent._apply_cache_to_history(api_messages),
                        output_config=reply_envelope.ANTHROPIC_OUTPUT_CONFIG,   # the reply is a declared field
                    ) as stream:
                        response = stream.get_final_message()
                    if cand != self.model:
                        print(f"[overload] Fell back to {cand} for this call.")
                    break
                except anthropic.APIStatusError as e:
                    last_err = e
                    if agent._is_context_window_error(e) and context_retries < 2:
                        api_messages = agent._compact_context_retry(api_messages, self.chat_id)
                        context_retries += 1
                        print(f"[context] Anthropic request exceeded model window; compacted history to "
                              f"{len(api_messages)} messages and retrying.")
                        continue
                    if e.status_code != 529:
                        raise
                    if overload_retries < 2:
                        wait = 5 * (2 ** overload_retries)
                        overload_retries += 1
                        print(f"{cand} overloaded, retrying in {wait}s (retry {overload_retries}/2)...")
                        time.sleep(wait)
                    else:
                        print(f"{cand} still overloaded; trying next model.")
                        break
            if response is not None:
                break
        if response is None:
            raise last_err

        # Usage per call: cache_read should dominate on warm turns; a large cache_write every turn means the prefix
        # is changing and the cache is being missed.
        u = getattr(response, "usage", None)
        agent._inbox.record_event("response", cand, "api", "anthropic", u)
        if u is not None:
            print(f"[usage] {cand}  in={getattr(u, 'input_tokens', 0)}  "
                  f"cache_write={getattr(u, 'cache_creation_input_tokens', 0) or 0}  "
                  f"cache_read={getattr(u, 'cache_read_input_tokens', 0) or 0}  "
                  f"out={getattr(u, 'output_tokens', 0)}")

        text, calls = "", []
        for block in response.content:
            if block.type == "text":
                text = block.text
            elif block.type == "tool_use":
                calls.append((block.id, block.name, block.input))
        stop = response.stop_reason
        if stop == "end_turn" or (stop == "tool_use" and calls):
            stop = ""
        # Sanitised so SDK-only fields cannot be echoed back and rejected by the API.
        return Exchange(text=text, blocks=agent._clean_content(response.content), calls=calls,
                        truncated=stop == "max_tokens", stop="" if stop == "max_tokens" else stop, history=api_messages)


class OpenAIResponses:
    """OpenAI Responses API, on the ChatGPT plan when signed in (falling back to the paid API at quota) or on the API."""

    def __init__(self, agent, chat_id):
        self.agent, self.chat_id = agent, chat_id
        self.ready = False
        self.continuation = ResponsesContext()

    def _setup(self):
        """The client, model and instructions. A str is an early reply that ends the turn."""
        agent, chat_id = self.agent, self.chat_id
        try:
            from openai import OpenAI as OpenAIClient
        except ImportError:
            return "openai package not installed. Run: pip install openai"
        self.OpenAIClient = OpenAIClient
        try:
            plan_token = None if (agent._routing.ACTIVE.get() or {}).get("kind") == "openai_api" else agent._chatgpt_plan.access_token()
        except Exception as e:
            if agent._routing.ACTIVE.get():
                raise agent._routing.Unavailable("ChatGPT sign-in needs attention") from e
            print(f"ChatGPT sign-in refresh failed: {e}")
            return "ChatGPT sign-in needs attention. Use /openai connect to reconnect; I did not switch to API billing."
        if (agent._routing.ACTIVE.get() or {}).get("kind") == "openai_subscription" and not plan_token:
            raise agent._routing.Unavailable("ChatGPT sign-in unavailable or subscription cooling down", 300)
        self.plan_mode = bool(plan_token)
        self.client = OpenAIClient(api_key=plan_token or agent.OPENAI_API_KEY, timeout=600.0,
                                   max_retries=0 if self.plan_mode else 2)
        self.api_model = agent.get_openai_model(chat_id)
        self.model = agent._routing.model("openai") or (agent.CHATGPT_PLAN_MODEL if self.plan_mode else self.api_model)
        route = ("GPT-6.1 Sol via the ChatGPT plan; GPT-6.1 Sol via OpenAI API if plan quota is reached."
                 if self.plan_mode else f"{self.api_model} via OpenAI API; ChatGPT plan access is not active for this turn.")
        if agent._routing.ACTIVE.get():
            route = agent._routing.label(agent._routing.ACTIVE.get())
        self.instructions = agent.SYSTEM_PROMPT + (
            f"\n\n[Runtime model: {route} If Alex asks which model is answering, state the active model clearly.]")
        self.tools = agent._oa_tools_for_responses()
        self.ready = True
        return None

    def exchange(self, api_messages):
        agent, chat_id = self.agent, self.chat_id
        if not self.ready:
            early = self._setup()
            if early:
                return early
        response = None
        context_retries = 0
        auth_retries = 0
        for attempt in range(6):
            try:
                request_items = self.continuation.prepare(api_messages, agent._history_to_responses_items, self.model)
                response, streamed_text, streamed_items, stream_events = agent._responses_call(
                    self.client, model=self.model, instructions=self.instructions,
                    input_items=request_items, tools=self.tools,
                    plan_mode=self.plan_mode)
                break
            except agent._ChatGPTQuotaReached:
                if not self.plan_mode:
                    raise
                agent._chatgpt_plan.mark_quota_exhausted()
                if agent._routing.ACTIVE.get():
                    raise agent._routing.Unavailable("ChatGPT subscription usage limit reached", 5 * 3600, quota=True)
                if not agent.OPENAI_API_KEY:
                    return "ChatGPT usage limit reached, and no OpenAI API key is configured for fallback."
                print("[billing] ChatGPT plan quota reached; switching this and later calls to GPT-6.1 API.")
                self.plan_mode = False
                self.model = self.api_model
                self.instructions = agent.SYSTEM_PROMPT + (
                    f"\n\n[Runtime model: {self.model} via OpenAI API; ChatGPT plan quota was reached. "
                    "If Alex asks which model is answering, state the active model clearly.]")
                self.client = self.OpenAIClient(api_key=agent.OPENAI_API_KEY, timeout=600.0, max_retries=2)
                try:
                    agent.send_message(chat_id, f"ChatGPT subscription limit reached. I'm continuing this task "
                                                f"with {self.model} via the paid OpenAI API.")
                except Exception as notice_error:
                    print(f"[billing] Could not deliver fallback notice: {type(notice_error).__name__}")
                continue  # Same task/history; do not repeat completed tool actions.
            except Exception as e:
                if self.plan_mode and getattr(e, "status_code", None) == 401 and auth_retries < 1:
                    refreshed = agent._chatgpt_plan.access_token(force_refresh=True)
                    if not refreshed:
                        raise
                    self.client = self.OpenAIClient(api_key=refreshed, timeout=600.0, max_retries=0)
                    auth_retries += 1
                    continue  # An expired login token is not a subscription-quota event.
                if agent._is_context_window_error(e) and context_retries < 2:
                    api_messages = agent._compact_context_retry(api_messages, chat_id)
                    context_retries += 1
                    print(f"[context] Request exceeded model window; compacted history to {len(api_messages)} messages and retrying.")
                    continue
                status = getattr(e, "status_code", None)
                if status in (429, 500, 502, 503) and attempt < 2:
                    wait = 10 * (2 ** attempt)
                    print(f"OpenAI {status}, retrying in {wait}s (attempt {attempt + 1}/3)...")
                    time.sleep(wait)
                else:
                    raise
        if response is None:
            raise RuntimeError("Model request could not complete within the bounded retry budget; history is preserved.")
        if self.plan_mode:
            try:
                if agent._chatgpt_plan.mark_plan_available() and not agent._routing.ACTIVE.get():
                    print("[billing] Subscription request succeeded; returned to ChatGPT plan usage.")
                    agent.send_message(chat_id, f"ChatGPT subscription is available again. I'm back on "
                                                f"{self.model} through your subscription.")
            except Exception as notice_error:
                print(f"[billing] Could not record or notify subscription recovery: {type(notice_error).__name__}")
        u = getattr(response, "usage", None)
        agent._inbox.record_event("response", self.model, "subscription" if self.plan_mode else "api", "openai", u)
        if u is not None:
            from agent_core.usage import normalize
            measured = normalize('openai', u)
            billing = "chatgpt-plan" if self.plan_mode else "api"
            print(f"[usage] {billing} {self.model} provider-reported {json.dumps(measured)}")

        output_items = response.output or streamed_items
        text = "".join(c.text for o in output_items if o.type == "message"
                       for c in o.content if getattr(c, "type", "") == "output_text") or streamed_text
        raw_calls = [o for o in output_items if o.type == "function_call"]
        truncated = (response.status == "incomplete"
                     and getattr(response.incomplete_details, "reason", "") == "max_output_tokens")
        if not raw_calls and not text.strip() and not truncated:
            output_types = ",".join(getattr(item, "type", "?") for item in output_items)
            content_types = ",".join(getattr(block, "type", "?") for item in output_items
                                     if getattr(item, "type", "") == "message" for block in getattr(item, "content", []))
            print(f"[empty_response] model={self.model} status={response.status} "
                  f"output_types={output_types or 'none'} content_types={content_types or 'none'} "
                  f"stream_events={','.join(stream_events[-12:]) or 'none'}")
        # Persist in the canonical (Anthropic) block format.
        blocks = [{"type": "text", "text": reply_envelope.readable(text)}] if text.strip() else []
        calls = []
        for c in raw_calls:
            try:
                args = json.loads(c.arguments or "{}")
            except Exception:
                args = {}
            name = c.name.rsplit(".", 1)[-1]
            blocks.append({"type": "tool_use", "id": c.call_id, "name": name, "input": args})
            calls.append((c.call_id, name, args))
        if calls and not truncated:
            self.continuation.remember(api_messages, blocks, request_items, output_items, self.model)
        return Exchange(text=text, blocks=blocks, calls=calls, truncated=truncated, history=api_messages)
