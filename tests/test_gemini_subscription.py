"""Offline checks for Gemini subscription routing, loopback MCP bridge, usage, allowance and native search."""
import http.client
import io
import json
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from tabularium.core import reply_envelope
from tabularium.core.agent_runtime import DurableInbox
from tabularium.models import provider_routing as routing
from tabularium.models import subscription_runtime as bridge
from tabularium.models import subscription_usage as allowance
from tabularium.records import task_cost
from tabularium.tools import search_routing

ROOT = Path(__file__).resolve().parents[1]


def _fake_agy_popen(script_fn):
    """Build a subprocess.Popen replacement whose stdin/stdout speak agy's stream-json protocol."""
    def factory(cmd, **kwargs):
        out_r, out_w = io.StringIO(), []
        stdin_buf = io.StringIO()

        class Stdin:
            def write(self, data):
                stdin_buf.write(data)

            def flush(self):
                pass

            def close(self):
                pass

        lines = []
        done = threading.Event()
        state = {"rc": 0, "stderr": ""}

        def run():
            try:
                # Wait briefly for prompt write
                deadline = time.time() + 2
                while not stdin_buf.getvalue() and time.time() < deadline:
                    time.sleep(0.01)
                events, rc, err = script_fn(cmd, kwargs, stdin_buf.getvalue())
                state["rc"] = rc
                state["stderr"] = err
                for ev in events:
                    lines.append(json.dumps(ev) + "\n")
            finally:
                done.set()

        t = threading.Thread(target=run, daemon=True)
        t.start()

        class Stdout:
            def __init__(self):
                self.idx = 0

            def readline(self):
                while True:
                    if self.idx < len(lines):
                        line = lines[self.idx]
                        self.idx += 1
                        return line
                    if done.is_set():
                        return ""
                    time.sleep(0.01)

            def close(self):
                pass

        class Stderr:
            def read(self):
                done.wait(timeout=2)
                return state["stderr"]

            def close(self):
                pass

        class Proc:
            stdin = Stdin()
            stdout = Stdout()
            stderr = Stderr()

            @property
            def returncode(self):
                return state["rc"] if done.is_set() else None

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                done.wait(timeout=timeout)
                return state["rc"]

            def terminate(self):
                done.set()

            def kill(self):
                done.set()

        return Proc()

    return factory


class GeminiRouting(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.agent = NS(
            AGENT_DIR=self.root, RUNTIME_DIR=self.root, _review_counts={},
            _strip_old_images=Mock(), OPENAI_API_KEY="fake", ANTHROPIC_API_KEY="fake",
            send_message=Mock(), run_turn_anthropic=Mock(return_value="Claude API"),
            run_turn_openai=Mock(return_value="OpenAI"),
        )

    def test_five_route_sequence_and_model_prefix_validation(self):
        five = [
            {"kind": "gemini_subscription", "model": "gemini-4-carbon-high"},
            {"kind": "openai_subscription", "model": "gpt-6.1-sol"},
            {"kind": "anthropic_subscription", "model": "claude-opus-5-5"},
            {"kind": "openai_api", "model": "gpt-6.1-sol"},
            {"kind": "anthropic_api", "model": "claude-sonnet-5-5"},
        ]
        saved = routing.save(self.root, {"enabled": True, "routes": five})
        self.assertEqual(len(saved["routes"]), 5)
        self.assertEqual(routing.label(five[0]), "gemini-4-carbon-high · Gemini subscription")
        for bad in (
            [{"kind": "gemini_subscription", "model": "gpt-6.1-sol"}],
            [{"kind": "gemini_subscription", "model": "claude-sonnet-5-5"}],
            [{"kind": "openai_subscription", "model": "gemini-4-carbon-high"}],
            [{"kind": "gemini_api", "model": "gemini-4-carbon-high"}],
            five + [{"kind": "gemini_subscription", "model": "gemini-3.8-flash-high"}],
        ):
            with self.assertRaises(ValueError):
                routing.validate({"enabled": True, "routes": bad})

    def test_gemini_route_executes_and_quota_falls_back_with_single_notice(self):
        routing.save(self.root, {"enabled": True, "routes": [
            {"kind": "gemini_subscription", "model": "gemini-4-carbon-high"},
            {"kind": "openai_api", "model": "gpt-6.1-sol"},
        ]})
        with patch("tabularium.models.subscription_runtime.run_gemini", return_value="Gemini reply") as gm:
            self.assertEqual(routing.run(self.agent, [], 1), "Gemini reply")
            gm.assert_called_once_with(self.agent, [], 1, "gemini-4-carbon-high")

        with patch("tabularium.models.subscription_runtime.run_gemini",
                   side_effect=routing.Unavailable("Gemini subscription usage limit reached", 18000, quota=True)):
            self.assertEqual(routing.run(self.agent, [], 1), "OpenAI")
        self.agent.send_message.assert_called_once()
        self.assertIn("Gemini subscription usage limit reached", self.agent.send_message.call_args.args[1])
        cooldown = routing.read(self.root / "task_state" / "route_gemini_subscription.json")
        self.assertEqual(cooldown["model"], "gemini-4-carbon-high")
        self.assertTrue(cooldown["quota"])


class GeminiBridge(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        self._lock_dir = Path(temp.name)
        for target, val in (
            ("tabularium.models.gemini_subscription.GeminiStartLock.PATH", self._lock_dir / "gemini_start.lock"),
            ("tabularium.models.gemini_subscription.AGY_HOME", self._lock_dir / "agy_home"),
        ):
            p = patch(target, val)
            p.start()
            self.addCleanup(p.stop)

    def _make_agent(self, folder):
        inbox = DurableInbox(folder, lambda *args: None)
        return NS(
            AGENT_DIR=folder, RUNTIME_DIR=folder, SYSTEM_PROMPT="Own persona",
            MAX_TOOL_ITERATIONS=4, _abort_requested={},
            get_tools=lambda _: [{
                "name": "read_memory", "description": "Read core memory",
                "input_schema": {"type": "object", "properties": {"section": {"type": "string"}}},
            }],
            _background_boundary=Mock(return_value=[]),
            _prepare_request_context=lambda m, c: m,
            _sanitise_tool_history=lambda m: m,
            trim_history=lambda m, c: m,
            save_conversation=Mock(),
            execute_tool=Mock(return_value="Memory receipt 456"),
            _inbox=inbox,
            _background=NS(newest=lambda c: 0, activated_since=lambda c, t: False),
            BACKGROUND_TASK_ID="",
        )

    def test_loopback_mcp_runs_only_owned_tools_on_turn_thread_and_records_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            agent = self._make_agent(folder)
            turn_tid = threading.get_ident()
            tool_threads = []

            def run_tool(name, args, chat):
                tool_threads.append(threading.get_ident())
                return f"Completed {name}:{args.get('section', '')}"

            agent.execute_tool.side_effect = run_tool
            captured = {}

            def script(cmd, kwargs, stdin_text):
                captured["cmd"] = cmd
                captured["env"] = kwargs["env"]
                captured["cwd"] = Path(kwargs["cwd"])
                captured["flags"] = kwargs.get("creationflags", 0)
                captured["stdin"] = json.loads(stdin_text.strip())
                mcp_cfg = json.loads((captured["cwd"] / ".agents" / "mcp_config.json").read_text(encoding="utf-8"))
                srv = mcp_cfg["mcpServers"]["agent"]
                captured["agent_md"] = (captured["cwd"] / ".agents" / "agents" / "tabularium" / "agent.md").read_text(encoding="utf-8")
                captured["has_git_stop"] = (captured["cwd"] / ".git").is_dir()
                # Unauthenticated call must be rejected
                host_port = srv["serverUrl"].split("://", 1)[1].split("/", 1)[0]
                host, port = host_port.split(":")
                c = http.client.HTTPConnection(host, int(port), timeout=5)
                c.request("POST", "/mcp", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
                          {"Content-Type": "application/json"})
                captured["unauth_status"] = c.getresponse().status
                c.close()

                def rpc(mid, method, params=None):
                    conn = http.client.HTTPConnection(host, int(port), timeout=5)
                    conn.request("POST", "/mcp",
                                 json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}),
                                 {"Content-Type": "application/json", **srv["headers"]})
                    resp = conn.getresponse()
                    data = json.loads(resp.read())
                    conn.close()
                    return data["result"]

                init_res = rpc(1, "initialize", {"protocolVersion": "2025-03-26"})
                tools_res = rpc(2, "tools/list")
                captured["listed_tools"] = [t["name"] for t in tools_res["tools"]]
                call_res = rpc(3, "tools/call", {"name": "read_memory", "arguments": {"section": "goals"}})
                captured["call_res"] = call_res

                envelope = {
                    "reply": "All done", "modality": "text", "work": "none", "task_outcome": "none",
                    "question": "", "decision_task_id": 0, "decision_id": 0, "status_claims": [],
                    "correction": [], "not_correction": [], "answers": [], "request": "done",
                }
                return ([
                    {"event": "init", "conversation_id": "conv-1"},
                    {"event": "step_update", "step_update": {"state": "DONE", "step_type": "agent_response",
                     "usage": {"input_tokens": 1200, "cache_read_tokens": 800, "output_tokens": 40, "total_tokens": 1240}}},
                    {"event": "result", "result": {
                        "conversation_id": "conv-1", "status": "SUCCESS",
                        "response": json.dumps(envelope), "structured_output": envelope,
                        "usage": {"input_tokens": 2000, "cache_read_tokens": 1500, "output_tokens": 90,
                                  "thinking_tokens": 30, "total_tokens": 2090},
                    }},
                ], 0, "")

            messages = [{"role": "user", "content": "Check memory"}]
            with patch.object(bridge, "find_gemini_cli", return_value="agy.exe"), \
                 patch("subprocess.Popen", side_effect=_fake_agy_popen(script)):
                reply = bridge.run_gemini(agent, messages, 1, "gemini-4-carbon-high")

            self.assertEqual(reply, "All done")
            self.assertEqual(captured["env"]["GEMINI_API_KEY"], "")
            self.assertEqual(captured["env"]["GOOGLE_API_KEY"], "")
            self.assertEqual(captured["env"]["GOOGLE_GENAI_USE_VERTEXAI"], "")
            self.assertTrue(captured["has_git_stop"])
            self.assertEqual(captured["unauth_status"], 403)
            self.assertEqual(captured["listed_tools"], ["read_memory"])
            self.assertEqual(tool_threads, [turn_tid])
            self.assertIn("Completed read_memory:goals", captured["call_res"]["content"][0]["text"])
            self.assertIn("enableMcpTools: true", captured["agent_md"])
            self.assertIn("- finish", captured["agent_md"])
            # 2026-11-15 17:41: under the harness's "finish once the task is complete" framing the model worked sixty
            # tool rounds without replying. The definition opens with the session contract, before the persona.
            body = captured["agent_md"].split("---\n", 2)[2]
            self.assertTrue(body.startswith("# This session\n"), body[:80])
            self.assertIn("`finish` call delivers your reply and ends the turn", body)
            self.assertLess(body.index("# This session"), body.index("Own persona"))
            self.assertEqual(messages[1]["content"][0]["id"], messages[2]["content"][0]["tool_use_id"])
            self.assertEqual(messages[2]["content"][0]["content"], "Completed read_memory:goals")
            self.assertEqual(agent.save_conversation.call_count, 2)
            spend = task_cost.spend(folder)
            self.assertEqual((spend["input_total"], spend["cache_read"], spend["output"]), (2000, 1500, 90))

    def test_auth_and_quota_errors_map_to_unavailable_unless_tool_is_uncertain(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._make_agent(Path(tmp))
            def auth_fail(cmd, kwargs, stdin_text):
                return ([{"event": "result", "result": {"status": "ERROR", "error": "authentication required"}}], 1, "authentication required")

            with patch.object(bridge, "find_gemini_cli", return_value="agy.exe"), \
                 patch("subprocess.Popen", side_effect=_fake_agy_popen(auth_fail)):
                with self.assertRaises(routing.Unavailable) as ctx:
                    bridge.run_gemini(agent, [{"role": "user", "content": "Hi"}], 1, "gemini-4-carbon-high")
                self.assertIn("sign-in", str(ctx.exception).lower())

            def quota_fail(cmd, kwargs, stdin_text):
                return ([{"event": "result", "result": {"status": "ERROR", "error": "RESOURCE_EXHAUSTED: quota limit reached"}}], 1, "")

            with patch.object(bridge, "find_gemini_cli", return_value="agy.exe"), \
                 patch("subprocess.Popen", side_effect=_fake_agy_popen(quota_fail)):
                with self.assertRaises(routing.Unavailable) as ctx:
                    bridge.run_gemini(agent, [{"role": "user", "content": "Hi"}], 1, "gemini-4-carbon-high")
                self.assertTrue(ctx.exception.quota)

    def test_a_long_prompt_and_a_talkative_cli_do_not_deadlock_on_the_pipes(self):
        # 2026-11-15 16:16: Cato's first Gemini turn wrote a 500 KB prompt into agy's stdin before anything read agy's
        # stdout and stderr; agy filled its output pipe, both sides blocked for good, the supervisor killed the agent
        # and the owner's message was lost. Real pipes and a real child here: the readers run before the prompt is fed.
        import subprocess as sp, sys
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._make_agent(Path(tmp))
            envelope = {"reply": "All done", "modality": "text", "work": "none", "task_outcome": "none", "question": "",
                        "decision_task_id": 0, "decision_id": 0, "status_claims": [], "correction": [], "not_correction": [],
                        "answers": [], "request": "done"}
            fake = Path(tmp) / "fake_agy.py"
            fake.write_text(
                "import json, sys\n"
                "sys.stderr.write('x' * 400_000); sys.stderr.flush()          # more than any pipe buffer, before reading stdin\n"
                "line = sys.stdin.readline()\n"
                f"env = {json.dumps(envelope)}\n"
                "for ev in [{'event': 'init', 'conversation_id': 'c1'},\n"
                "           {'event': 'step_update', 'step_update': {'state': 'DONE', 'step_type': 'agent_response',\n"
                "            'usage': {'input_tokens': 10, 'cache_read_tokens': 0, 'output_tokens': 5, 'total_tokens': 15}}},\n"
                "           {'event': 'result', 'result': {'conversation_id': 'c1', 'status': 'SUCCESS', 'response': json.dumps(env),\n"
                "            'structured_output': env, 'usage': {'input_tokens': 10, 'cache_read_tokens': 0, 'output_tokens': 5, 'total_tokens': 15}}}]:\n"
                "    sys.stdout.write(json.dumps(ev) + '\\n'); sys.stdout.flush()\n", encoding="utf-8")
            real_popen = sp.Popen
            messages = [{"role": "user", "content": "filler " * 80_000}]       # a 500 KB prompt, like a day's transcript
            out = {}

            def run():
                with patch.object(bridge, "find_gemini_cli", return_value="agy.exe"), \
                     patch("subprocess.Popen", side_effect=lambda argv, **kw: real_popen([sys.executable, "-X", "utf8", str(fake)] + list(argv[1:]), **kw)):
                    out["reply"] = bridge.run_gemini(agent, messages, 1, "gemini-4-carbon-high")

            worker = threading.Thread(target=run, daemon=True); worker.start(); worker.join(timeout=60)
            self.assertFalse(worker.is_alive(), "the route deadlocked on its own pipes")
            self.assertEqual(out.get("reply"), "All done")

    def test_concurrent_turns_use_distinct_git_isolated_temp_sessions_and_isolated_home(self):
        from tabularium.models import gemini_subscription as gs
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            agy_home = folder / "isolated_agy_home"
            agent = self._make_agent(folder)
            barrier = threading.Barrier(2, timeout=10)
            seen = []
            seen_lock = threading.Lock()
            envelope = {
                "reply": "OK", "modality": "text", "work": "none", "task_outcome": "none",
                "question": "", "decision_task_id": 0, "decision_id": 0, "status_claims": [],
                "correction": [], "not_correction": [], "answers": [], "request": "done",
            }

            def concurrent_script(cmd, kwargs, stdin_text):
                cwd = Path(kwargs["cwd"])
                def stream():
                    cid = f"conv-{threading.get_ident()}"
                    yield {"event": "init", "conversation_id": cid}
                    barrier.wait()
                    head = (cwd / ".git" / "HEAD").read_text(encoding="utf-8") if (cwd / ".git" / "HEAD").is_file() else ""
                    settings = json.loads((cwd / "settings.json").read_text(encoding="utf-8"))
                    mcp_cfg = json.loads((cwd / ".agents" / "mcp_config.json").read_text(encoding="utf-8"))
                    with seen_lock:
                        seen.append({
                            "cwd": cwd,
                            "head": head,
                            "settings": settings,
                            "port": mcp_cfg["mcpServers"]["agent"]["serverUrl"],
                            "env": kwargs["env"],
                        })
                    yield {"event": "result", "result": {
                        "conversation_id": cid, "status": "SUCCESS",
                        "response": json.dumps(envelope), "structured_output": envelope,
                        "usage": {"input_tokens": 10, "cache_read_tokens": 0, "output_tokens": 2},
                    }}
                return (stream(), 0, "")

            with patch.object(bridge, "find_gemini_cli", return_value="agy.exe"), \
                 patch.object(gs, "AGY_HOME", agy_home, create=True), \
                 patch("subprocess.Popen", side_effect=_fake_agy_popen(concurrent_script)):
                errs = []
                def call_one(chat_id):
                    try:
                        bridge.run_gemini(agent, [{"role": "user", "content": "Hi"}], chat_id, "gemini-4-carbon-medium")
                    except BaseException as exc:
                        errs.append(exc)
                t1 = threading.Thread(target=call_one, args=(1,), daemon=True)
                t2 = threading.Thread(target=call_one, args=(2,), daemon=True)
                t1.start(); t2.start()
                t1.join(timeout=15); t2.join(timeout=15)
                self.assertEqual(errs, [])

            self.assertEqual(len(seen), 2)
            self.assertNotEqual(seen[0]["cwd"], seen[1]["cwd"])
            self.assertNotEqual(seen[0]["port"], seen[1]["port"])
            for item in seen:
                self.assertFalse(item["cwd"].is_relative_to(folder))
                self.assertFalse(item["cwd"].exists(), "temp session dir must be removed on exit")
                self.assertIn("ref: refs/heads/", item["head"])
                self.assertIs(item["settings"].get("useG1Credits"), False)
                self.assertEqual(Path(item["env"]["USERPROFILE"]), agy_home)
                self.assertEqual(Path(item["env"]["HOME"]), agy_home)

    def test_pre_init_log_only_failure_classifies_auth_and_cleans_up_conversation_files(self):
        from tabularium.models import gemini_subscription as gs
        cid = "11111111-2222-3333-4444-555555555555"
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            agy_home = folder / "agy_home"
            cli_root = agy_home / ".gemini" / "antigravity-cli"
            brain_dir = cli_root / "brain" / cid
            conv_db = cli_root / "conversations" / f"{cid}.db"
            lock_file = cli_root / "presence" / f"{cid}.lock"
            agent = self._make_agent(folder)

            def crash_before_init(cmd, kwargs, stdin_text):
                self.assertIn("--log-file", cmd)
                log_path = Path(cmd[cmd.index("--log-file") + 1])
                brain_dir.mkdir(parents=True, exist_ok=True)
                (brain_dir / "transcript.jsonl").write_text("{}", encoding="utf-8")
                conv_db.parent.mkdir(parents=True, exist_ok=True)
                conv_db.write_bytes(b"sqlite")
                lock_file.parent.mkdir(parents=True, exist_ok=True)
                lock_file.write_text("", encoding="utf-8")
                log_path.write_text(
                    f"I1115 16:16:17 manager.go:454] Created conversation {cid}\n"
                    "E1115 16:16:17 auth.go:91] error getting token source: You are not logged into Antigravity.\n",
                    encoding="utf-8",
                )
                return ([], 1, "")

            with patch.object(bridge, "find_gemini_cli", return_value="agy.exe"), \
                 patch.object(gs, "AGY_HOME", agy_home, create=True), \
                 patch("subprocess.Popen", side_effect=_fake_agy_popen(crash_before_init)):
                with self.assertRaises(routing.Unavailable) as ctx:
                    bridge.run_gemini(agent, [{"role": "user", "content": "Hi"}], 1, "gemini-4-carbon-medium")
                self.assertIn("sign-in", str(ctx.exception).lower())

            self.assertFalse(brain_dir.exists())
            self.assertFalse(conv_db.exists())
            self.assertFalse(lock_file.exists())

    def test_cleanup_kills_process_tree_when_cli_is_still_running(self):
        from tabularium.models import gemini_subscription as gs
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._make_agent(Path(tmp))
            agent._abort_requested[1] = False

            class HungProc:
                pid = 999999
                stdin = NS(write=lambda d: None, flush=lambda: None, close=lambda: None)
                stdout = NS(readline=lambda: (_ for _ in ()).throw(RuntimeError("boom")), close=lambda: None)
                stderr = NS(read=lambda: "", close=lambda: None)
                def poll(self): return None
                def wait(self, timeout=None): return 0

            with patch.object(bridge, "find_gemini_cli", return_value="agy.exe"), \
                 patch.object(gs, "AGY_HOME", Path(tmp) / "agy_home", create=True), \
                 patch("subprocess.Popen", return_value=HungProc()), \
                 patch.object(gs, "_kill_process_tree", create=True) as kill_tree:
                with self.assertRaises(RuntimeError):
                    bridge.run_gemini(agent, [{"role": "user", "content": "Hi"}], 1, "gemini-4-carbon-medium")
                kill_tree.assert_called_once()


class GeminiAllowanceAndSearch(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        self._lock_dir = Path(temp.name)
        for target, val in (
            ("tabularium.models.gemini_subscription.GeminiStartLock.PATH", self._lock_dir / "gemini_start.lock"),
            ("tabularium.models.gemini_subscription.AGY_HOME", self._lock_dir / "agy_home"),
        ):
            p = patch(target, val)
            p.start()
            self.addCleanup(p.stop)
    def test_gemini_usage_parses_windows_without_credentials(self):
        sample = {
            "status": "SUCCESS",
            "command": {"name": "usage", "data": {"groups": [{
                "name": "Gemini Models",
                "buckets": [
                    {"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.944, "reset_time": "2026-11-15T19:52:48Z"},
                    {"id": "gemini-weekly", "window": "weekly", "remaining_fraction": 0.98, "reset_time": "2026-11-22T14:52:48Z"},
                ],
            }]}},
        }
        completed = NS(returncode=0, stdout=json.dumps(sample), stderr="")
        with patch.object(allowance, "find_gemini_cli", return_value="agy.exe"), \
             patch("subprocess.run", return_value=completed) as run_mock:
            data = allowance.gemini_usage()
        self.assertTrue(data["available"])
        self.assertEqual([w["name"] for w in data["windows"]], ["Five-hour window", "Weekly allowance"])
        self.assertAlmostEqual(data["windows"][0]["remaining"], 94.4, places=1)
        self.assertAlmostEqual(data["windows"][1]["remaining"], 98.0, places=1)
        self.assertTrue(run_mock.call_args.kwargs.get("creationflags", 0) & getattr(subprocess, "CREATE_NO_WINDOW", 0)
                        or getattr(subprocess, "CREATE_NO_WINDOW", 0) == 0)

    def test_gemini_native_search_requires_search_web_tool_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            agent = NS(AGENT_DIR=folder, RUNTIME_DIR=folder, _inbox=NS(record_event=Mock()))

            def no_tool(cmd, kwargs, stdin_text):
                return ([{"event": "result", "result": {"status": "SUCCESS", "response": "From memory", "usage": {}}}], 0, "")

            with patch.object(bridge, "find_gemini_cli", return_value="agy.exe"), \
                 patch("subprocess.Popen", side_effect=_fake_agy_popen(no_tool)):
                with self.assertRaises(search_routing.NoEvidence):
                    search_routing.PROVIDERS["gemini_subscription"](agent, "test query", "gemini-3.8-flash-high")

            def with_tool(cmd, kwargs, stdin_text):
                return ([
                    {"event": "step_update", "step_update": {"state": "DONE", "step_type": "tool", "tool_name": "search_web"}},
                    {"event": "result", "result": {"status": "SUCCESS", "response": "Live answer https://example.com",
                                                    "usage": {"input_tokens": 100, "output_tokens": 20}}},
                ], 0, "")

            with patch.object(bridge, "find_gemini_cli", return_value="agy.exe"), \
                 patch("subprocess.Popen", side_effect=_fake_agy_popen(with_tool)):
                out = search_routing.PROVIDERS["gemini_subscription"](agent, "test query", "gemini-3.8-flash-high")
            self.assertIn("https://example.com", out)

    def test_gemini_native_search_does_not_deadlock_on_pipes_and_cleans_up_temp_session(self):
        import subprocess as sp, sys
        from tabularium.models import gemini_subscription as gs
        cid = "22222222-3333-4444-5555-666666666666"
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            agy_home = folder / "agy_home"
            conv_db = agy_home / ".gemini" / "antigravity-cli" / "conversations" / f"{cid}.db"
            conv_db.parent.mkdir(parents=True, exist_ok=True)
            conv_db.write_bytes(b"sqlite")
            agent = NS(AGENT_DIR=folder, RUNTIME_DIR=folder, _inbox=NS(record_event=Mock()))
            fake = folder / "fake_search_agy.py"
            fake.write_text(
                "import json, pathlib, sys\n"
                "args = sys.argv[1:]\n"
                "if '--log-file' in args:\n"
                f"    pathlib.Path(args[args.index('--log-file') + 1]).write_text('Created conversation {cid}\\n', encoding='utf-8')\n"
                "sys.stderr.write('e' * 400_000); sys.stderr.flush()\n"
                "line = sys.stdin.readline()\n"
                "for ev in [\n"
                "    {'event': 'step_update', 'step_update': {'state': 'DONE', 'step_type': 'tool', 'tool_name': 'search_web'}},\n"
                "    {'event': 'result', 'result': {'status': 'SUCCESS', 'response': 'Found https://example.org', 'usage': {'input_tokens': 50, 'output_tokens': 10}}},\n"
                "]:\n"
                "    sys.stdout.write(json.dumps(ev) + '\\n'); sys.stdout.flush()\n",
                encoding="utf-8",
            )
            real_popen = sp.Popen
            seen_cwds = []

            def launch(argv, **kw):
                seen_cwds.append(Path(kw["cwd"]))
                return real_popen([sys.executable, "-X", "utf8", str(fake)] + list(argv[1:]), **kw)

            box = {}
            def run():
                with patch.object(bridge, "find_gemini_cli", return_value="agy.exe"), \
                     patch.object(gs, "AGY_HOME", agy_home, create=True), \
                     patch("subprocess.Popen", side_effect=launch):
                    box["out"] = search_routing.PROVIDERS["gemini_subscription"](
                        agent, "query " * 20_000, "gemini-3.8-flash-high")

            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            worker.join(timeout=20)
            self.assertFalse(worker.is_alive(), "search_routing._gemini_subscription deadlocked on pipes")
            self.assertIn("https://example.org", box.get("out", ""))
            self.assertEqual(len(seen_cwds), 1)
            self.assertFalse(seen_cwds[0].is_relative_to(folder))
            self.assertFalse(seen_cwds[0].exists())
            self.assertFalse(conv_db.exists())


if __name__ == "__main__":
    unittest.main()
