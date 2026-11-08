"""Reproducible synthetic before/after benchmark; --live uses Cal's subscription, never API fallback.
Run: python -B tests/benchmark_token_efficiency.py --baseline PATH [--live]
No live memory/conversation edits. Reports contain only synthetic evidence and aggregate usage.
"""
import argparse
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import tiktoken
from agent_harness import make_agent
from agent_core import context_window as current
from agent_core import turn_loop
from agent_core.command_output import command_result
from agent_core.openai_chatgpt import ChatGPTPlan


def load(name, path):
    import sys
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    baseline = Path(args.baseline)
    old_context = load("baseline_context", baseline / "agent_core/context_window.py")
    old_loop = load("baseline_loop", baseline / "agent_core/turn_loop.py")
    old_shared = load("baseline_shared", baseline / "agent_core/shared_handlers.py")
    encoder = tiktoken.get_encoding("o200k_base")
    count = lambda text: len(encoder.encode(text))
    folder = Path(__file__).resolve().parents[1] / "cato/reports/token-efficiency"
    folder.mkdir(parents=True, exist_ok=True)
    scratch = tempfile.TemporaryDirectory()
    try:
        root = Path(scratch.name)
        agent = make_agent("callimachus", root)
        stdout = "".join(f"case_{i:04d} PASS: verified fixture record\n" for i in range(350))
        stdout += "FAIL: test_balance\nAssertionError: expected 73, observed 71\n"
        stdout += "".join(f"case_{i:04d} PASS: verified fixture record\n" for i in range(350, 700))
        stdout += "Ran 701 tests\nFAILED (failures=1)\n"
        stderr = "WARNING: optional diagnostics unavailable\n"
        old_output = agent._cap_result((stdout + "\nSTDERR:\n" + stderr).strip())
        new_output = command_result(stdout, stderr, 1, folder / "archives")
        compact = json.loads(new_output)
        data = {"baseline": str(baseline), "tokenizer": "o200k_base (local estimate, not provider billing)",
                "command": {"before_tokens": count(old_output), "after_tokens": count(new_output),
                            "before_failure_details_visible": "expected 73, observed 71" in old_output,
                            "after_failure_details_visible": "expected 73, observed 71" in new_output,
                            "after_explicit_exit": compact["exit"],
                            "archive_exact": Path(compact["archive"]).read_text(encoding="utf-8") == "STDOUT:\n" + stdout + "\nSTDERR:\n" + stderr}}
        # Same result sequence, isolates below-limit age rewriting from output size.
        for label, prepare in [("before", old_context.prepare_context), ("after", current.prepare_context)]:
            history = [{"role":"user","content":"fixture brief"}, {"role":"assistant","content":"ready"}]
            breaks = 0
            total = 0
            for i in range(10):
                previous = deepcopy(history)
                history += [{"role":"assistant","content":[{"type":"tool_use","id":str(i),"name":"read_file","input":{}}]},
                            {"role":"user","content":[{"type":"tool_result","tool_use_id":str(i),"content":("fixture data %d " % i)*300}]}]
                history = prepare(history, root / label, budget_bytes=160000)
                breaks += history[:len(previous)] != previous
                total += count(json.dumps(history,ensure_ascii=False))
            data[label + "_context"] = {"prefix_rewrites": breaks, "serialized_input_tokens": total}
        (folder / "comparison.json").write_text(json.dumps(data,indent=2),encoding="utf-8")
        print(json.dumps(data,indent=2),flush=True)
        if not args.live:
            return
        plan = ChatGPTPlan("Callimachus")
        if not plan.access_token():
            raise RuntimeError("Cal subscription sign-in unavailable")
        agent._chatgpt_plan = plan
        token = agent._routing.ACTIVE.set({"kind":"openai_subscription","model":"gpt-6.1-sol"})
        all_tools = agent.get_tools("anthropic")
        allowed = {"execute_command", "read_file", "search_archived"}
        agent.BASE_TOOLS_ANTHROPIC = [t for t in all_tools if t["name"] in allowed]
        # Normal schemas/converter, restricted to three fixture tools; no external action can execute.
        from agent_core.agent_app import openai_tool
        agent.BASE_TOOLS_OPENAI = [openai_tool(t) for t in agent.BASE_TOOLS_ANTHROPIC]
        agent.get_tools = lambda provider: agent.BASE_TOOLS_ANTHROPIC if provider == "anthropic" else agent.BASE_TOOLS_OPENAI
        original_execute = agent._execute_tool_unguarded
        agent._background_boundary = lambda *unused: []
        agent._turn_notes.clear()
        agent._load_core_memory = lambda: ""
        instruction = ("Synthetic benchmark. First call execute_command with command fixture-log. "
                       "After reading it, call execute_command with command fixture-policy. These are fixture lookups, not shell commands. "
                       "Then report whether deployment is allowed, the failing test name, expected and observed balance, and number of tests. "
                       "Read/search any archived evidence if needed. Reply in one sentence. Do not do anything else.")
        data["live"] = {}
        try:
            for label, module in [("before", old_loop), ("after", turn_loop)]:
                usage = []
                agent._repeat_calls.clear(); agent._unproductive_streak.clear()
                def execute(name, inputs, chat):
                    if name == "execute_command":
                        if inputs.get("command") == "fixture-log":
                            return old_output if label == "before" else new_output
                        if inputs.get("command") == "fixture-policy":
                            return "Deployment is allowed only if exit code is zero and there are no failing tests."
                        raise RuntimeError("Only fixture commands are permitted")
                    if name in ("read_file", "search_archived"):
                        return original_execute(name, inputs, chat)
                    raise RuntimeError("Unexpected benchmark action")
                agent._execute_tool_unguarded = execute
                prepare = old_context.prepare_context if label == "before" else current.prepare_context
                agent._prepare_request_context = lambda history, chat: prepare(history, root / label, budget_bytes=160000)
                send = old_shared.SharedHandlers._responses_call if label == "before" else type(agent)._responses_call
                def request(client, **kw):
                    if len(usage) >= 6:
                        raise RuntimeError("Benchmark exceeded six requests")
                    response, text, items, events = send(agent, client, **kw)
                    u = response.usage
                    usage.append({"input": u.input_tokens, "cached": getattr(u.input_tokens_details,"cached_tokens",0) or 0,
                                  "output": u.output_tokens,
                                  "reasoning": getattr(u.output_tokens_details,"reasoning_tokens",0) or 0})
                    print(label, "request", len(usage), usage[-1],flush=True)
                    return response, text, items, events
                agent._responses_call = request
                agent._cache_key = lambda: "cal-efficiency-benchmark-" + label
                start = time.monotonic()
                messages = [{"role":"user","content":instruction}]
                answer = module.run(agent,messages,1,module.OpenAIResponses(agent,1))
                quality = {"expected": "73" in answer, "observed": "71" in answer,
                           "test": "test_balance" in answer, "count": "701" in answer,
                           "blocked": any(word in answer.lower() for word in ["not allowed", "blocked", "not permitted", "cannot", "must not", "denied", "not be deployed"])}
                data["live"][label] = {"usage":usage,"answer":str(answer),"quality":quality,"seconds":round(time.monotonic()-start,1)}
                (folder / "comparison.json").write_text(json.dumps(data,indent=2),encoding="utf-8")
                print(label, 'answer:',answer, 'quality:',quality,flush=True)
        finally:
            agent._routing.ACTIVE.reset(token)
    finally:
        scratch.cleanup()


if __name__ == "__main__":
    main()
