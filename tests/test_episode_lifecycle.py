"""Episode lifecycle, within-episode prefix stability, read deduplication, and inline command output."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.agent_harness import make_agent, put
from tabularium.core.command_output import command_result
from tabularium.core.outcome import Failed
from tabularium.core.session_lifecycle import continue_turn
from tabularium.core.turn_loop import Exchange, OpenAIResponses, run
from tabularium.models.gemini_api import GeminiAPIError, GeminiGenerate
from tabularium.records import verification
WORK = [{"item": "Do what the brief asks", "check": "Read the result", "critical": True}]   # judged work is born with its contract


class EpisodeLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_turn_loop_preserves_prefix_and_native_reasoning_past_worker_wire_budget(self):
        agent = make_agent("engineer", self.root, background=True)
        agent._chatgpt_plan.access_token = lambda force_refresh=False: None
        files = []
        for idx in range(7):
            p = self.root / f"module_{idx}.py"
            p.write_text(f"# module {idx}\n" + (f"value_{idx} = {idx}\n" * 2200), encoding="utf-8")
            files.append(p)

        seen_inputs = []
        step = {"n": 0}

        def fake_responses_call(client, *, model, instructions, input_items, tools, plan_mode):
            seen_inputs.append(json.loads(json.dumps(input_items)))
            idx = step["n"]
            step["n"] += 1
            if idx < len(files):
                call_id = f"call_{idx}"
                out = [
                    SimpleNamespace(
                        type="reasoning",
                        id=f"rs_{idx}",
                        encrypted_content=f"enc_reasoning_{idx}",
                        summary=[],
                    ),
                    SimpleNamespace(
                        type="function_call",
                        call_id=call_id,
                        name="read_file",
                        arguments=json.dumps({"path": str(files[idx]), "offset": 0, "limit": 35000}),
                    ),
                ]
                resp = SimpleNamespace(output=out, status="completed", usage=None)
                return resp, "", [], []
            out = [
                SimpleNamespace(
                    type="message",
                    content=[SimpleNamespace(type="output_text", text='{"reply":"all modules inspected"}')],
                )
            ]
            return SimpleNamespace(output=out, status="completed", usage=None), "", [], []

        agent._responses_call = fake_responses_call
        messages = agent.load_memory_context() + [{"role": "user", "content": "Inspect all seven modules."}]
        agent.conversations[1] = messages
        reply = run(agent, messages, 1, OpenAIResponses(agent, 1))
        self.assertEqual(str(reply), "all modules inspected")
        self.assertGreaterEqual(len(seen_inputs), 8)
        # Total wire payload across 7 x ~31 KB reads exceeds WORKER_REQUEST_BYTES (160 KB),
        # yet within one episode the sent prefix must stay strictly append-only and keep step 0's native reasoning item.
        self.assertGreater(len(json.dumps(seen_inputs[-1])), agent.WORKER_REQUEST_BYTES)
        for prev, curr in zip(seen_inputs, seen_inputs[1:]):
            self.assertEqual(curr[: len(prev)], prev)
        self.assertTrue(any(item.get("encrypted_content") == "enc_reasoning_0" for item in seen_inputs[-1]))

    def test_read_file_deduplicates_unchanged_spans_and_refreshes_on_edit_or_compaction(self):
        agent = make_agent("engineer", self.root, background=True)
        target = self.root / "scheduler.py"
        original_body = "".join(f"line_{i:04d} = {i}\n" for i in range(300))
        target.write_text(original_body, encoding="utf-8")

        first = agent.execute_tool("read_file", {"path": str(target), "offset": 0, "limit": 8000}, 1)
        self.assertEqual(first, original_body)

        # Exact repeat and narrower sub-span both return a compact reference instead of re-sending the file.
        second = agent.execute_tool("read_file", {"path": str(target), "offset": 0, "limit": 8000}, 1)
        self.assertIn("Unchanged since read #1", second)
        self.assertLess(len(second), 160)

        subspan = agent.execute_tool("read_file", {"path": str(target), "offset": 100, "limit": 400}, 1)
        self.assertIn("Unchanged since read #1", subspan)

        # Runtime verification uses _execute_tool_unguarded and must still read raw file contents.
        store = agent._background
        tid = store.start(1, "Check scheduler", completion_items=[
            {"item": "scheduler defines line_0010", "check": "scheduler.py contains line_0010 = 10", "critical": True}])["id"]
        put(store, tid, "running")
        agent.BACKGROUND_TASK_ID = str(tid)
        store.checkpoint(
            1,
            tid,
            "Checked scheduler",
            "Verify",
            [],
            completion_items=[
                {
                    "source": 0,
                    "item": "scheduler defines line_0010",
                    "check": "scheduler.py contains line_0010 = 10",
                    "critical": True,
                    "verification": {
                        "kind": "contains",
                        "paths": [str(target)],
                        "text": "line_0010 = 10",
                    },
                }
            ],
        )
        verdict = json.loads(verification.execute(agent, 1, tid, 1))
        self.assertEqual(verdict["status"], "passed")

        # Editing the file invalidates the digest so the next read_file returns fresh contents.
        edited_body = original_body.replace("line_0010 = 10", "line_0010 = 999")
        target.write_text(edited_body, encoding="utf-8")
        after_edit = agent.execute_tool("read_file", {"path": str(target), "offset": 0, "limit": 8000}, 1)
        self.assertEqual(after_edit, edited_body)

        # Emergency context-window compaction clears _episode_reads so compacted-away text can be re-read.
        padded = [
            {"role": "user", "content": "seed"},
            {"role": "assistant", "content": "ack"},
        ]
        for idx in range(6):
            padded += [
                {"role": "assistant", "content": [{"type": "tool_use", "id": f"u{idx}", "name": "read_file", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"u{idx}", "content": "x" * 10000}]},
            ]
        agent._compact_context_retry(padded, 1)
        after_compact = agent.execute_tool("read_file", {"path": str(target), "offset": 0, "limit": 8000}, 1)
        self.assertEqual(after_compact, edited_body)

    def test_command_result_inlines_up_to_16k_and_archives_larger_outputs(self):
        medium = ("step ok\n" * 1400) + "AssertionError: expected 42 got 0\n"
        self.assertGreater(len(medium), 10000)
        self.assertLess(len(medium), 16000)
        inline_res = command_result(medium, "", 1, self.root)
        self.assertIsInstance(inline_res, Failed)
        inline_data = json.loads(inline_res)
        self.assertEqual(inline_data["stdout"], medium)
        self.assertNotIn("archive", inline_data)
        self.assertEqual(list(self.root.glob("*.txt")), [])

        large = ("verbose line\n" * 1500) + "AssertionError: expected 42 got 0\n"
        self.assertGreater(len(large), 16000)
        archived_res = command_result(large, "", 1, self.root)
        archived_data = json.loads(archived_res)
        self.assertIn("archive", archived_data)
        self.assertIn("expected 42 got 0", archived_data["excerpt"])

    def test_session_limit_archives_episode_resets_messages_and_preserves_state_packet(self):
        agent_root = self.root / "vit"
        agent_root.mkdir()
        repo = agent_root / "sample_repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.test"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=repo, check=True, capture_output=True)
        tracked = repo / "worker_mod.py"
        tracked.write_text("STATUS = 'old'\n", encoding="utf-8")
        subprocess.run(["git", "add", "worker_mod.py"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

        agent = make_agent("engineer", agent_root, background=True)
        agent.MAX_TOOL_ITERATIONS = 3
        store = agent._background
        ident = store.start(1, "Repair worker_mod", f"Target repository: {repo}", completion_items=[
            {"item": "worker_mod sets fixed status", "check": "worker_mod.py contains STATUS = 'fixed'", "critical": True}])["id"]
        put(store, ident, "running")
        agent.BACKGROUND_TASK_ID = str(ident)
        agent.RUNTIME_DIR = store.directory(ident)
        agent._inbox.directory = agent.RUNTIME_DIR / "task_state"

        messages = store.episode_seed(ident, 1, agent.load_memory_context())
        agent.conversations[1] = messages
        seen_episode_two = []
        state = {"step": 0}

        class ScriptedProvider:
            def exchange(self, api_messages):
                idx = state["step"]
                state["step"] += 1
                if idx == 0:
                    call = ("c1", "write_file", {"path": str(tracked), "content": "STATUS = 'fixed'\n"})
                    return Exchange(
                        blocks=[{"type": "tool_use", "id": "c1", "name": "write_file", "input": call[2]}],
                        calls=[call],
                    )
                if idx == 1:
                    store.control(1, ident, "steer", "Keep backward compatibility for STATUS.")
                    cp_inputs = {
                        "completed": "Updated worker_mod.py",
                        "next_action": "Acknowledge steer and verify",
                        "evidence": [str(tracked)],
                        "facts": ["worker_mod.py sets STATUS='fixed'"],
                        "completion_items": [
                            {
                                "source": 0,
                                "item": "worker_mod sets fixed status",
                                "check": "worker_mod.py contains STATUS = 'fixed'",
                                "critical": True,
                                "verification": {
                                    "kind": "contains",
                                    "paths": [str(tracked)],
                                    "text": "STATUS = 'fixed'",
                                },
                            },
                        ],
                    }
                    return Exchange(
                        blocks=[{"type": "tool_use", "id": "c2", "name": "task_checkpoint", "input": cp_inputs}],
                        calls=[("c2", "task_checkpoint", cp_inputs)],
                    )
                if idx == 2:
                    # Step 2 runs after _background_boundary applied scope update #1.
                    # Also verify that completion_items=[] on a progress-only checkpoint succeeds without erasing items!
                    cp_steer = {
                        "completed": "Added steer requirement",
                        "next_action": "Verify both items",
                        "evidence": [str(tracked)],
                        "completion_items": [
                            {
                                "source": 1,
                                "item": "steer constraint honoured",
                                "check": "worker_mod.py retains STATUS symbol",
                                "critical": True,
                                "verification": {
                                    "kind": "contains",
                                    "paths": [str(tracked)],
                                    "text": "STATUS",
                                },
                            },
                        ],
                    }
                    cp_empty = {
                        "completed": "Verified module and saved progress",
                        "next_action": "",
                        "evidence": [str(tracked)],
                        "completion_items": [],
                    }
                    return Exchange(
                        blocks=[
                            {"type": "tool_use", "id": "c3s", "name": "task_checkpoint", "input": cp_steer},
                            {"type": "tool_use", "id": "c3a", "name": "verify_requirement", "input": {"task_id": ident, "item": 1}},
                            {"type": "tool_use", "id": "c3b", "name": "verify_requirement", "input": {"task_id": ident, "item": 2}},
                            {"type": "tool_use", "id": "c3c", "name": "task_checkpoint", "input": cp_empty},
                        ],
                        calls=[
                            ("c3s", "task_checkpoint", cp_steer),
                            ("c3a", "verify_requirement", {"task_id": ident, "item": 1}),
                            ("c3b", "verify_requirement", {"task_id": ident, "item": 2}),
                            ("c3c", "task_checkpoint", cp_empty),
                        ],
                    )
                seen_episode_two.append(json.loads(json.dumps(api_messages)))
                return Exchange(text='{"reply":"Repair completed across session boundary.","task_outcome":"completed"}')

        result = continue_turn(agent, messages, 1, lambda: run(agent, messages, 1, ScriptedProvider()))
        self.assertEqual(str(result), "Repair completed across session boundary.")

        # Episode 1 raw tool messages were archived to disk, and Episode 2 ran with a clean messages list.
        ep1_path = store.directory(ident) / "task_state" / "episodes" / "episode_001.json"
        self.assertTrue(ep1_path.exists())
        ep1 = json.loads(ep1_path.read_text(encoding="utf-8"))
        self.assertIn("Updated worker_mod.py", json.dumps(ep1["messages"]))
        self.assertEqual(len(seen_episode_two), 1)
        wire_ep2 = json.dumps(seen_episode_two[0])
        # No raw tool_use blocks from Episode 1 remain in Episode 2's messages, yet contract + actions + git status survive:
        self.assertNotIn('"id": "c1"', wire_ep2)
        self.assertIn("Keep backward compatibility for STATUS.", wire_ep2)
        self.assertIn("Target repository:", wire_ep2)
        self.assertIn("worker_mod.py", wire_ep2)
        self.assertIn("write_file", wire_ep2)
        self.assertIn("episode_001.json", wire_ep2)
        self.assertEqual(verification.status(store, ident, "", 1), "passed")
        self.assertEqual(verification.status(store, ident, "", 2), "passed")

    def test_episode_with_only_redundant_unchanged_reads_stops_at_session_limit(self):
        agent = make_agent("engineer", self.root, background=True)
        agent.MAX_TOOL_ITERATIONS = 2
        store = agent._background
        put(store, 1, "running")
        (store.directory(1) / "context.json").write_text(json.dumps({"context": ""}), encoding="utf-8")
        sample = self.root / "sample.py"
        sample.write_text("x = 1\n", encoding="utf-8")

        # Seed _episode_reads? Even simpler: Episode 1 reads sample.py on step 0 and writes a note on step 1 (real progress),
        # then Episode 2 reads sample.py at step 0 (fresh in Episode 2 -> real progress), so to have an episode with ONLY
        # redundant reads: step 0 reads sample.py? Wait: in a single episode of MAX_TOOL_ITERATIONS=2, if sample.py was
        # pre-recorded in _episode_reads OR if we test 2 steps where step 0 was already read: wait - on a fresh episode,
        # read #1 of sample.py IS new to that episode unless we test MAX_TOOL_ITERATIONS=2 after pre-reading or test
        # unproductive + unchanged read! Wait: what if an episode reads sample.py once and then repeats it 59 times?
        # Even better: let's check both! If an episode only ever re-reads files already in _episode_reads (or fails tools),
        # _episode_progress is False and continue_turn stops.
        messages = store.episode_seed(1, 1, agent.load_memory_context())
        agent.conversations[1] = messages
        # Pre-populate _episode_reads for chat 1 as if already shown in this episode before the remaining steps:
        first = agent.execute_tool("read_file", {"path": str(sample)}, 1)
        self.assertEqual(first, "x = 1\n")

        class RedundantReadProvider:
            def __init__(self):
                self.n = 0

            def exchange(self, api_messages):
                self.n += 1
                # Alternate offsets inside the already-read span so _repeat_calls does not fire first.
                inp = {"path": str(sample), "offset": 0, "limit": 100 + self.n}
                cid = f"r{self.n}"
                return Exchange(
                    blocks=[{"type": "tool_use", "id": cid, "name": "read_file", "input": inp}],
                    calls=[(cid, "read_file", inp)],
                )

        with self.assertRaisesRegex(RuntimeError, "without completed tool progress"):
            continue_turn(agent, messages, 1, lambda: run(agent, messages, 1, RedundantReadProvider()))

    def test_worker_restart_archives_prior_episode_and_resets_seed(self):
        from tabularium.core.roster import ROLES
        from tabularium.records.background_runtime import TaskStore, worker

        agent_root = self.root / "vit_restart"
        agent_root.mkdir()
        (agent_root / "SKILL.md").write_text("# Identity\nBe brief.\n", encoding="utf-8")
        (agent_root / "memory.md").write_text("# Memory\n\n## Core\n- test\n", encoding="utf-8")
        style_file = agent_root / "skills" / "talk-like-a-human-being" / "runtime-core.md"
        style_file.parent.mkdir(parents=True, exist_ok=True)
        style_file.write_text("Speak plainly.\n", encoding="utf-8")
        store = TaskStore(agent_root, ROLES["engineer"])
        started = store.start(1, "Repair stuck worker job", "Repository path: sample_repo", completion_items=WORK)
        ident = started["id"]
        claimed = store.claim_next()
        self.assertEqual(claimed["id"], ident)
        folder = store.directory(ident)

        # Simulate a prior crashed/paused episode with tool blocks already saved in conversation_1.json.
        prior = [
            {"role": "user", "content": "memory seed"},
            {"role": "assistant", "content": "memory ack"},
            {"role": "user", "content": "Repair stuck worker job"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "old_call_1", "name": "read_file", "input": {"path": "x.py"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "old_call_1", "content": "OLD_EPISODE_PAYLOAD_" * 500}]},
        ]
        (folder / "conversation_1.json").write_text(json.dumps(prior), encoding="utf-8")

        captured = []

        def fake_run_turn(self_agent, msgs, chat_id):
            captured.append(json.loads(json.dumps(self_agent.trim_history(msgs, chat_id))))
            return "Finished cleanly after restart."

        env = {
            "ANTHROPIC_API_KEY": "test",
            "OPENAI_API_KEY": "test",
            "SEARCH_PROVIDER": "anthropic",
            "ENGINEER_BACKGROUND_TASK": str(ident),
            "ENGINEER_AGENTMAIL_API_KEY": "am_test",
            "TELEGRAM_BOT_TOKEN": "1:a",
            "PRIVATE_TELEGRAM_BOT_TOKEN": "2:b",
            "LIBRARIAN_TELEGRAM_BOT_TOKEN": "3:c",
            "ENGINEER_TELEGRAM_BOT_TOKEN": "4:d",
        }
        with patch.dict("os.environ", env), patch("tabularium.core.engine.AgentRuntime.run_turn", fake_run_turn):
            worker(ident, agent_root, ROLES["engineer"], claimed["attempt"])

        ep_path = folder / "task_state" / "episodes" / "episode_001.json"
        self.assertTrue(ep_path.exists())
        archived = json.loads(ep_path.read_text(encoding="utf-8"))
        self.assertEqual(archived["reason"], "worker_restart")
        self.assertIn("OLD_EPISODE_PAYLOAD_", json.dumps(archived["messages"]))
        self.assertEqual(len(captured), 1)
        wire = json.dumps(captured[0])
        self.assertNotIn("OLD_EPISODE_PAYLOAD_", wire)
        self.assertIn("Repository path: sample_repo", wire)
        self.assertIn("episode_001.json", wire)

    def test_openai_and_gemini_retry_http_504(self):
        agent = make_agent("engineer", self.root, background=False)
        agent._chatgpt_plan.access_token = lambda force_refresh=False: None

        class GatewayTimeout(Exception):
            status_code = 504

        oa_calls = {"n": 0}

        def flaky_oa(client, *, model, instructions, input_items, tools, plan_mode):
            oa_calls["n"] += 1
            if oa_calls["n"] == 1:
                raise GatewayTimeout("504 Gateway Time-out")
            out = [SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", text='{"reply":"recovered oa"}')])]
            return SimpleNamespace(output=out, status="completed", usage=None), "", [], []

        agent._responses_call = flaky_oa
        with patch("tabularium.core.turn_loop.time.sleep"):
            ex = OpenAIResponses(agent, 1).exchange([{"role": "user", "content": "ping"}])
        self.assertEqual(oa_calls["n"], 2)
        self.assertIn("recovered oa", ex.text)

        agent.GEMINI_API_KEY = "test-gemini-key"
        gm = GeminiGenerate(agent, 1)
        gm_calls = {"n": 0}

        def flaky_gm(body):
            gm_calls["n"] += 1
            if gm_calls["n"] == 1:
                raise GeminiAPIError(504, "Gateway Timeout")
            return {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": '{"reply":"recovered gm"}'}]}}]}

        gm.call = flaky_gm
        with patch("tabularium.models.gemini_api.time.sleep"):
            ex_gm = gm.exchange([{"role": "user", "content": "ping"}])
        self.assertEqual(gm_calls["n"], 2)
        self.assertIn("recovered gm", ex_gm.text)


if __name__ == "__main__":
    unittest.main()
