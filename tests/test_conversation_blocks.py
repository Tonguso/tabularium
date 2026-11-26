"""What was sent on an earlier turn goes again unchanged, and only while the conversation still begins with it (2026-11-26)."""
import json
import unittest
from tabularium.models import conversation_blocks as cb

U = lambda text: {"role": "user", "content": text}
A = lambda text: {"role": "assistant", "content": text}
STATE = dict(core="[core]\nHe lives in local.", evidence="", turn='[Incoming messages: [{"id": 1}]]', notes="")


def turn(log, messages, stable=None, state=STATE, retry=None, budget=300_000, wire=None):
    return cb.compose(log, messages, wire or messages, len(messages) if stable is None else stable, state, retry, budget, now=0)


class Blocks(unittest.TestCase):
    def test_the_next_turn_repeats_the_last_one_byte_for_byte_and_adds_one_block(self):
        first = [U("seed"), A("ready"), U("Book the flight")]
        texts1, log = turn(dict(cb.EMPTY), first)
        self.assertEqual(len(texts1), 2)                                        # the core memory, then the history
        self.assertIn("He lives in local.", texts1[0])
        self.assertIn("Book the flight", texts1[1])
        second = first + [A("Booked."), U("And the hotel")]
        texts2, log2 = turn(log, second)
        self.assertEqual(texts2[:2], texts1)                                    # what the cache holds is sent again unchanged
        self.assertEqual(len(texts2), 3)
        self.assertNotIn("Book the flight", texts2[2])
        self.assertEqual(json.loads(texts2[2].split("]\n", 1)[1].split("\n[With the latest", 1)[0]), second[3:])
        self.assertEqual(len(log2["fps"]), 5)

    def test_a_changed_earlier_message_rebuilds_the_request_whole(self):
        first = [U("seed"), A("ready"), U("Book the flight")]
        _, log = turn(dict(cb.EMPTY), first)
        moved = [U("seed"), A("ready"), U("Book the train"), A("Booked."), U("And the hotel")]      # not what was sent
        texts, log2 = turn(log, moved)
        self.assertEqual(len(texts), 2)
        self.assertIn("Book the train", texts[1])
        self.assertNotIn("Book the flight", "".join(texts))
        shorter = [U("seed"), A("ready")]                                       # the window moved past what was recorded
        self.assertEqual(len(turn(log2, shorter)[1]["fps"]), 2)

    def test_the_exchange_in_progress_and_a_retry_note_are_sent_and_never_recorded(self):
        first = [U("seed"), A("ready"), U("Build it")]
        texts1, log = turn(dict(cb.EMPTY), first)
        working = first + [A([{"type": "tool_use", "id": "t", "name": "write_file", "input": {}}]),
                           U([{"type": "tool_result", "tool_use_id": "t", "content": "saved"}])]
        texts2, log2 = turn(log, working, stable=3, retry=U("Answer in the declared format."))
        self.assertEqual(texts2[:2], texts1)
        self.assertIn("saved", texts2[2])
        self.assertIn("declared format", texts2[2])
        self.assertEqual(log2, log)                                             # nothing of this attempt is kept
        done = first + [A("[Completed tool trace: x.json]"), A("Built."), U("Thanks")]            # how the next turn holds it
        texts3, _ = turn(log2, done)
        self.assertEqual(texts3[:2], texts1)
        self.assertNotIn("declared format", "".join(texts3))

    def test_the_task_evidence_goes_only_when_it_changed_and_says_when_none_is_left(self):
        base = [U("seed"), A("ready"), U("one")]
        busy = dict(STATE, evidence='[Live task evidence]\n{"tasks": [{"task_id": 7, "checked_at": 1795000000.5}]}')
        texts, log = turn(dict(cb.EMPTY), base, state=busy)
        self.assertIn('"task_id": 7', texts[1])
        again = dict(STATE, evidence='[Live task evidence]\n{"tasks": [{"task_id": 7, "checked_at": 1795000999.1}]}')
        texts, log = turn(log, base + [A("ok"), U("two")], state=again)
        self.assertNotIn("task_id", texts[2])                                   # the same state read later: not repeated
        texts, log = turn(log, base + [A("ok"), U("two"), A("ok"), U("three")], state=STATE)
        self.assertIn(cb.NO_EVIDENCE, texts[3])                                 # an earlier block still shows task 7
        texts, log = turn(log, base + [A("ok"), U("two"), A("ok"), U("three"), A("ok"), U("four")], state=STATE)
        self.assertNotIn("Live task evidence", texts[4])

    def test_past_the_budget_the_history_is_one_block_again_made_from_the_bounded_copy(self):
        long = [U("seed"), A("ready"), U("x" * 500)]
        _, log = turn(dict(cb.EMPTY), long)
        grown = long + [A("y" * 500), U("next")]
        bounded = [U("seed"), A("ready"), U("x" * 20 + "..."), A("y" * 20 + "..."), U("next")]
        texts, log2 = turn(log, grown, budget=900, wire=bounded)
        self.assertEqual(len(texts), 2)
        self.assertNotIn("x" * 500, texts[1])
        self.assertEqual(log2["fps"], [cb.fingerprint(m) for m in grown])       # the fingerprints are of the conversation itself

    def test_a_lost_or_damaged_record_is_an_empty_one(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(cb.load(Path(tmp) / "none.json"), cb.EMPTY)
            (Path(tmp) / "bad.json").write_text('{"fps": 3}', encoding="utf-8")
            self.assertEqual(cb.load(Path(tmp) / "bad.json"), cb.EMPTY)


class TheRouteSendsThem(unittest.TestCase):
    def test_two_turns_of_the_owner_share_their_first_blocks_and_a_worker_keeps_one_block(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace as NS
        from unittest.mock import Mock, patch
        import claude_agent_sdk as sdk
        from tabularium.models import subscription_runtime as bridge
        with tempfile.TemporaryDirectory() as folder:
            def attach(m, c, attach=True):
                agent._current = dict(STATE)
                return m
            agent = NS(AGENT_DIR=Path(folder), RUNTIME_DIR=Path(folder), SYSTEM_PROMPT="persona", MAX_TOOL_ITERATIONS=4,
                       _abort_requested={}, _background_boundary=lambda *a: [], get_tools=lambda _: [],
                       _prepare_request_context=lambda m, c: m, _sanitise_tool_history=lambda m: m, trim_history=attach,
                       save_conversation=Mock(), execute_tool=Mock(), _inbox=NS(progress=Mock(), record_event=Mock()),
                       _background=NS(newest=lambda c: 0, activated_since=lambda c, t: False), BACKGROUND_TASK_ID="")
            sent = []

            async def query(prompt, options):
                async for message in prompt:
                    sent.append([b["text"] for b in message["message"]["content"]])
                yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                                        session_id="fake", result="", structured_output={"reply": "Done"})
            patches = [patch.object(bridge, "find_cli", return_value="fake.exe"),
                       patch.object(bridge, "auth_status", return_value={"loggedIn": True, "authMethod": "claude.ai"}),
                       patch.object(sdk, "create_sdk_mcp_server", return_value={}), patch.object(sdk, "query", side_effect=query)]
            for p in patches: p.start()
            try:
                messages = [U("seed"), A("ready"), U("Book the flight")]
                bridge.run_claude(agent, messages, 1, "claude-test")                # appends the reply
                messages.append(U("And the hotel"))
                bridge.run_claude(agent, messages, 1, "claude-test")
                self.assertEqual(sent[1][:2], sent[0])
                self.assertEqual(len(sent[1]), 3)
                self.assertIn("And the hotel", sent[1][2])
                agent.BACKGROUND_TASK_ID = "7"                                      # a task's own history: one block, as before
                with self.assertRaises(AttributeError):                             # the stand-in has no task store to finish in
                    bridge.run_claude(agent, [U("seed"), A("ready"), U("Do the task")], 1, "claude-test")
                self.assertEqual(len(sent[2]), 1)
                self.assertIn("Do the task", sent[2][0])
            finally:
                for p in reversed(patches): p.stop()


if __name__ == "__main__":
    unittest.main()
