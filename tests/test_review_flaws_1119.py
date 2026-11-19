"""Flaws found in the 2026-11-19 review of the other Claude chat's fixes (6507cbd) and Gemini's episode redesign
(f7e3609), each pinned by the behaviour it lacked.

1. The runtime's own words: the crash notice was made bilingual; the inbox's failure and restart notices were not.
2. Nothing bounded a turn's request between its start and the provider's refusal.
3. A skill already loaded in the episode was loaded into the request again, and a new episode dropped every skill.
4. "Already shown" results were recognised by their first words; their kind is now their type."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from telebot.types import Message

from tabularium.channels.owner_telegram import OWNER_CHAT_ID
from tabularium.core.agent_runtime import DurableInbox, runtime_words, RUNTIME_WORDS
from tabularium.core.context_window import wire_size
from tabularium.core.engine import AgentRuntime
from tabularium.core.outcome import AlreadyShown


def message(i, text):
    return Message.de_json({'message_id': i, 'date': 1700000000, 'chat': {'id': OWNER_CHAT_ID, 'type': 'private'},
                            'from': {'id': OWNER_CHAT_ID, 'is_bot': False, 'first_name': 'Owner'}, 'text': text})


class RuntimeWords(unittest.TestCase):
    def test_every_notice_has_both_languages_and_follows_the_message(self):
        for kind in RUNTIME_WORDS:
            self.assertIn("I", runtime_words(kind, "Please check the report"))
            self.assertRegex(runtime_words(kind, "帮我看一下这个报告"), r"[一-鿿]")

    def test_a_failed_chinese_message_is_answered_in_chinese(self):
        with tempfile.TemporaryDirectory() as tmp:
            sent, done = [], threading.Event()
            inbox = DurableInbox(Path(tmp), lambda chat, text: sent.append(text) or done.set())
            try:
                @inbox.handler
                def handle_message(m):
                    raise RuntimeError("boom")
                handle_message(message(1, "看下这个，我们之前研究过用分钟线去算买盘和卖盘力量，还记得吗"))
                inbox.start()
                self.assertTrue(done.wait(6))
                self.assertEqual(sent, [RUNTIME_WORDS['failed'][1]])
            finally:
                inbox.close()

    def test_a_restart_speaks_the_language_of_the_interrupted_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            sent = []
            first = DurableInbox(Path(tmp), lambda chat, text: sent.append(text))
            @first.handler
            def handle_message(m):
                pass
            handle_message(message(1, "帮我把周报整理一下"))
            with first.db() as db:
                db.execute("UPDATE jobs SET status='running'")         # as a process killed mid-turn leaves it
            first.close()
            second = DurableInbox(Path(tmp), lambda chat, text: sent.append(text))
            try:
                second.start()
                self.assertEqual(sent, [RUNTIME_WORDS['restarted'][1]])
            finally:
                second.close()


def runtime(root):
    agent = AgentRuntime.__new__(AgentRuntime)
    agent.AGENT_DIR = Path(root)
    agent._episode_reads = {}
    agent.discover_skills = lambda: {"method": {"path": Path(root) / "skills/method/SKILL.md", "body": "Do it carefully."}}
    return agent


class StepCeiling(unittest.TestCase):
    def test_a_request_grows_untouched_then_is_cut_once_past_the_ceiling(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = runtime(tmp)
            agent.STEP_CEILING_BYTES = 40_000
            history = [{"role": "user", "content": "Task"}, {"role": "assistant", "content": "ok"}]
            for i in range(12):
                history += [{"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "read_file", "input": {"path": f"f{i}"}}]},
                            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "x" * 5000}]}]
            small = history[:10]
            self.assertIs(agent._bound_request(small, 1), small)          # under the ceiling: the very same request
            agent._episode_state(1)["spans"][("f", "d")] = [(0, 10, 1)]
            agent._episode_state(1)["skills"]["method"] = "digest"
            cut = agent._bound_request(history, 1)
            self.assertLessEqual(wire_size(cut), 40_000)
            self.assertEqual(len(cut), len(history))                      # results become previews; nothing is dropped
            self.assertEqual(agent._episode_state(1)["spans"], {})        # earlier spans may be previews now
            self.assertEqual(agent._episode_state(1)["skills"], {"method": "digest"})   # skills are never cut


class Skills(unittest.TestCase):
    def test_a_loaded_skill_is_not_loaded_again_in_the_episode(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = runtime(tmp)
            first = agent._dedup_episode_skill(1, {"name": "method"}, "# Skill: method\nDo it carefully.")
            again = agent._dedup_episode_skill(1, {"name": "method"}, "# Skill: method\nDo it carefully.")
            self.assertEqual(first, "# Skill: method\nDo it carefully.")
            self.assertIsInstance(again, AlreadyShown)
            agent.discover_skills = lambda: {"method": {"path": "p", "body": "Do it differently."}}
            changed = agent._dedup_episode_skill(1, {"name": "method"}, "# Skill: method\nDo it differently.")
            self.assertNotIsInstance(changed, AlreadyShown)                # an edited skill loads again

    def test_a_new_episode_opens_with_the_skills_the_last_one_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = runtime(tmp)
            agent._dedup_episode_skill(1, {"name": "method"}, "# Skill: method\nDo it carefully.")
            held = agent.loaded_skills(1)
            agent._episode_reads.pop(1)                                   # what the rollover does
            seed = [{"role": "user", "content": "Continue from the current task contract."}]
            agent.carry_skills(1, seed, held)
            self.assertIn("Do it carefully.", seed[-1]["content"])
            self.assertIsInstance(agent._dedup_episode_skill(1, {"name": "method"}, "# Skill: method\nDo it carefully."),
                                  AlreadyShown)


class Progress(unittest.TestCase):
    def test_a_session_of_already_shown_results_is_not_progress(self):
        from tabularium.core import session_lifecycle
        from tabularium.core.session_lifecycle import SessionLimit
        calls = {"n": 0}
        messages = [{"role": "user", "content": "Task"}]

        def run():
            calls["n"] += 1
            messages.extend([{"role": "assistant", "content": [{"type": "tool_use", "id": f"s{calls['n']}", "name": "skill", "input": {"name": "m"}}]},
                             {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"s{calls['n']}",
                                                           "content": AlreadyShown("[Skill m is already loaded above]")}]}])
            raise SessionLimit("API tool-round session limit")
        with tempfile.TemporaryDirectory() as tmp:
            agent = NS(RUNTIME_DIR=Path(tmp), save_conversation=lambda c: None, _inbox=NS(record_event=lambda *a, **k: None))
            (Path(tmp) / "task_state" / "actions").mkdir(parents=True)
            with self.assertRaisesRegex(RuntimeError, "without completed tool progress"):
                session_lifecycle.continue_turn(agent, messages, 1, run)
        self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main()
