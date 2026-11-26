"""A release answers one real turn per agent before any process is replaced (2026-11-25).

On 2026-11-24 a change passed both suites, was released, and every turn on the Claude subscription route then failed
for an hour: the provider refused the request. The suites replace the model with a stand-in, the release only checked
that processes were up, and the daily self-test sent one message with no conversation before it, so it would have
passed too. Now the self-test turn follows a conversation with a tool call in it, and the release runs that turn for
each agent, in its own process on the tested source, before it drains anything (ops/release.prove, tabularium probe).
These tests replace only the model call and the child processes."""
import io, json, os, subprocess, sys, tempfile, time, unittest
from contextlib import redirect_stdout
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch
from agent_harness import load_agent
from tabularium import cli
from tabularium.core.roster import AGENTS
from tabularium.models import provider_routing
from tabularium.ops import agent_control, release, supervisor


class SelftestTurn(unittest.TestCase):
    def runtime(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "coordinator"; root.mkdir()
        return load_agent("coordinator", root)["runtime"]

    def test_the_turn_follows_a_conversation_with_a_tool_call(self):
        runtime = self.runtime()
        seen = []

        def fake_run_turn(messages, chat_id):
            seen.extend(messages)
            messages.append({"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "x"}}]})
            return "OK"
        runtime.run_turn = fake_run_turn
        self.assertTrue(runtime.run_selftest()["ok"])
        self.assertGreater(len(seen), 3)
        self.assertTrue(any(isinstance(m["content"], list) and any(b.get("type") == "tool_result" for b in m["content"]) for m in seen))
        self.assertIn("Runtime self-test, not a message", seen[-1]["content"])

    def test_with_a_follow_up_a_second_message_is_answered_in_the_same_conversation(self):
        runtime = self.runtime()
        seen = []

        def fake_run_turn(messages, chat_id):
            seen.append(len(messages))
            if len(seen) == 1:
                messages.append({"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "x"}}]})
                messages.append({"role": "assistant", "content": "OK"})
                return "OK"
            self.assertIn("once more", messages[-1]["content"])
            return "Not that"
        runtime.run_turn = fake_run_turn
        record = runtime.run_selftest(follow_up=True)
        self.assertEqual(seen[1], seen[0] + 3)                    # the first turn's call and reply, then the second message
        self.assertEqual((record["ok"], record["error"]), (False, "the second turn's reply was not OK"))
        self.assertEqual(record["cache"], [])                     # the stand-in made no model call

    def test_the_tool_call_in_the_history_does_not_count_as_this_turns(self):
        runtime = self.runtime()
        runtime.run_turn = lambda messages, chat_id: "OK"          # answers without calling read_file
        record = runtime.run_selftest()
        self.assertFalse(record["ok"])
        self.assertEqual(record["error"], "no tool call")

    def test_each_run_starts_from_the_same_history(self):
        runtime = self.runtime()
        lengths = []

        def fake_run_turn(messages, chat_id):
            lengths.append(len(messages))
            messages[0]["content"] = "changed"
            return "OK"
        runtime.run_turn = fake_run_turn
        runtime.run_selftest(); runtime.run_selftest()
        self.assertEqual(lengths[0], lengths[1])
        self.assertNotEqual(runtime.SELFTEST_HISTORY[0]["content"], "changed")


class ProbeProcess(unittest.TestCase):
    def test_a_probe_is_the_foreground_agent_with_its_own_runtime_folder_and_no_port(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "coordinator"; root.mkdir()
        folder = Path(tmp.name) / "probe"; (folder / "task_state").mkdir(parents=True)
        (root / "SKILL.md").write_text("# Test identity\n", encoding="utf-8")
        (root / "memory.md").write_text("# Memory\n\n## Core\n- test\n", encoding="utf-8")
        import agent_harness
        (root / agent_harness.STYLE).parent.mkdir(parents=True)
        (root / agent_harness.STYLE).write_text("Answer briefly.\n", encoding="utf-8")
        env = {"ANTHROPIC_API_KEY": "test", "OPENAI_API_KEY": "test", "TELEGRAM_BOT_TOKEN": "1:a", "TABULARIUM_PROBE_DIR": str(folder)}
        from tabularium.agents import build
        from tabularium.core import engine
        with patch.dict(os.environ, env), patch.dict(os.environ, {"COORDINATOR_BACKGROUND_TASK": ""}), \
                patch.object(engine.AgentRuntime, "_claim_single_instance", side_effect=AssertionError("a probe must not claim the agent's port")):
            runtime = build(root, "coordinator")
        self.assertEqual(runtime.RUNTIME_DIR, folder)
        self.assertEqual(runtime.AGENT_DIR, root.resolve())
        self.assertFalse(runtime.BACKGROUND_TASK_ID)
        runtime.run_turn = lambda messages, chat_id: "OK"
        runtime.run_selftest()
        self.assertTrue((folder / "task_state" / "selftest.json").exists())
        self.assertFalse((root / "task_state" / "selftest.json").exists())

    def command(self, record, expected):
        agent = type("Agent", (), {"AGENT_DIR": Path("x"), "run_selftest": lambda self, follow_up=False: dict(record, asked_twice=follow_up)})()
        out = io.StringIO()
        with patch("tabularium.agents.build", return_value=agent), patch.object(provider_routing, "first_open", return_value=expected), \
                patch.dict(os.environ, {}), redirect_stdout(out):
            message = cli.probe(["coordinator", "somewhere"])
        line = [l for l in out.getvalue().splitlines() if l.startswith("PROBE ")][-1]
        return message, json.loads(line[6:])

    def test_an_answer_on_the_expected_route_passes(self):
        message, record = self.command({"ok": True, "error": "", "route": "anthropic_subscription"}, {"kind": "anthropic_subscription", "model": "claude-x"})
        self.assertIsNone(message)
        self.assertTrue(record["ok"])
        self.assertTrue(record["asked_twice"])                    # the probe sends a second message in the conversation

    def test_an_answer_from_a_fallback_fails(self):
        message, record = self.command({"ok": True, "error": "", "route": "openai_subscription"}, {"kind": "anthropic_subscription", "model": "claude-x"})
        self.assertIn("anthropic_subscription did not answer", message)
        self.assertFalse(record["ok"])

    def test_a_failed_turn_fails(self):
        message, record = self.command({"ok": False, "error": "API Error: 400", "route": ""}, {"kind": "anthropic_subscription", "model": "claude-x"})
        self.assertIn("API Error: 400", message)

    def test_unknown_slot_is_a_usage_error(self):
        self.assertIn("usage", cli.probe(["nobody", "x"]))


class FirstOpenRoute(unittest.TestCase):
    def test_a_cooling_route_is_passed_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "task_state").mkdir()
            (root / "routing.json").write_text(json.dumps({"enabled": True, "routes": [
                {"kind": "anthropic_subscription", "model": "claude-a"}, {"kind": "openai_subscription", "model": "gpt-b"}]}), encoding="utf-8")
            self.assertEqual(provider_routing.first_open(root)["kind"], "anthropic_subscription")
            (root / "task_state" / "route_anthropic_subscription.json").write_text(
                json.dumps({"model": "claude-a", "retry_at": time.time() + 300}), encoding="utf-8")
            self.assertEqual(provider_routing.first_open(root)["kind"], "openai_subscription")
            (root / "task_state" / "route_openai_subscription.json").write_text(
                json.dumps({"model": "gpt-b", "retry_at": time.time() + 300}), encoding="utf-8")
            self.assertIsNone(provider_routing.first_open(root))


class Child:
    def __init__(self, output, code=0, hang=False):
        self.output, self.returncode, self.hang, self.pid, self.killed = output, code, hang, 4242, False

    def communicate(self, timeout=None):
        if self.hang and timeout is not None:
            raise subprocess.TimeoutExpired("probe", timeout)
        return self.output, None

    def kill(self):
        self.killed = True


class Prove(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "logs").mkdir()
        self.calls = []
        p = patch.object(release, "snapshot", return_value=self.root / ".release/x/src"); p.start(); self.addCleanup(p.stop)

    def prove(self, children, agents=("coordinator", "engineer")):
        queue = list(children)

        def popen(argv, **kwargs):
            self.calls.append((argv, kwargs))
            return queue.pop(0)
        with patch("subprocess.Popen", side_effect=popen), patch("subprocess.run"):
            release.prove(self.root, {"commit": "x", "digest": "x"}, list(agents))

    def test_every_agent_answering_passes_on_the_release_source(self):
        ok = 'startup noise\nPROBE {"ok": true, "error": "", "route": "anthropic_subscription"}\n'
        self.prove([Child(ok), Child(ok)])
        self.assertEqual([c[0][-2] for c in self.calls], ["coordinator", "engineer"])
        self.assertEqual(self.calls[0][0][-3], "probe")
        self.assertEqual(self.calls[0][1]["env"]["PYTHONPATH"].split(os.pathsep)[0], str(self.root / ".release/x/src"))
        self.assertTrue((self.root / "coordinator/task_state/probe/task_state").is_dir())
        self.assertIn("probe coordinator: passed", (self.root / "logs/release-tests.log").read_text(encoding="utf-8"))

    def test_one_agent_failing_fails_the_release_and_names_it(self):
        ok = 'PROBE {"ok": true, "error": ""}\n'
        bad = 'Traceback\nPROBE {"ok": false, "error": "API Error: 400 A maximum of 4 blocks"}\n'
        with self.assertRaises(RuntimeError) as caught:
            self.prove([Child(ok), Child(bad, code=1)])
        self.assertIn("engineer: API Error: 400", str(caught.exception))
        self.assertNotIn("coordinator:", str(caught.exception))
        self.assertIn("Traceback", (self.root / "logs/release-tests.log").read_text(encoding="utf-8"))

    def test_a_probe_that_ends_without_a_result_fails(self):
        with self.assertRaises(RuntimeError) as caught:
            self.prove([Child("crashed before the turn\n", code=1)], agents=("coordinator",))
        self.assertIn("coordinator: the probe ended without a result", str(caught.exception))

    def test_a_probe_that_never_answers_is_stopped_and_fails(self):
        child = Child("", hang=True)
        with self.assertRaises(RuntimeError) as caught:
            self.prove([child], agents=("coordinator",))
        self.assertTrue(child.killed)
        self.assertIn("no answer in", str(caught.exception))


class Gate(unittest.TestCase):
    def test_the_release_job_certifies_the_agents_and_not_the_dashboard(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        target = {"commit": "tested", "digest": "abc"}
        submitted = []
        with patch.object(supervisor, "ROOT", root), patch.object(release, "identity", return_value=target), \
                patch.object(release, "uncommitted", return_value=[]):
            agent_control.request_restart(root=root)
            supervisor.release_step([supervisor.Child(n) for n in AGENTS + ("dashboard",)], None,
                                    lambda *a: submitted.append(a) or Future())
        self.assertEqual(submitted[0][0], release.certify)
        self.assertEqual(submitted[0][3], list(AGENTS))

    def test_a_failed_probe_leaves_nothing_drained(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        target = {"commit": "tested", "digest": "abc"}
        with patch.object(release, "test_version", return_value=target), \
                patch.object(release, "prove", side_effect=RuntimeError("A real turn failed on this release; coordinator: API Error: 400")):
            future = Future()
            try:
                future.set_result(release.certify(root, target, ["coordinator"]))
            except RuntimeError as exc:
                future.set_exception(exc)
        with patch.object(supervisor, "ROOT", root), patch.object(release, "identity", return_value=target), \
                patch.object(release, "uncommitted", return_value=[]):
            agent_control.request_restart(root=root)
            children = [supervisor.Child(n) for n in AGENTS + ("dashboard",)]
            job = supervisor.release_step(children, None, lambda *a: future)
            supervisor.release_step(children, job, lambda *a: future)
            record = supervisor.read_json(root / "dashboard/restart_status.json")
        self.assertEqual(record["status"], "failed")
        self.assertIn("A real turn failed", record["message"])
        self.assertFalse(any(root.glob("*/task_state/drain")))


if __name__ == "__main__":
    unittest.main()
