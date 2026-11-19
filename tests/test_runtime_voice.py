"""The agent speaks; the runtime does not (2026-11-19). Alex sent Cato a video and got "Processing video..." from the
runtime before Cato said a word, and other paths answered him in system language: "Video processing failed: ...",
"File type '.foo' not supported", "Voice error: ...", "Task #2163 stopped with an error". When something about his
message cannot be passed on as it is, the agent is told what he sent and what became of it, and replies itself. The
runtime writes only when the agent cannot run at all, and then in plain first-person words."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

from telebot.types import Message

from tabularium.channels.owner_telegram import OWNER_CHAT_ID
from tabularium.core.agent_runtime import DurableInbox
from tabularium.core.shared_handlers import FileTooBig, SharedHandlers
from tabularium.tools import video_intake


def message(i=1, **extra):
    raw = {'message_id': i, 'date': 1700000000, 'chat': {'id': OWNER_CHAT_ID, 'type': 'private'},
           'from': {'id': OWNER_CHAT_ID, 'is_bot': False, 'first_name': 'Owner'}, 'text': f'request {i}'}
    raw.update(extra)
    return Message.de_json(raw)


class Agent:
    """The handlers under test with everything around them recorded: what the agent was told, what was sent."""
    def __init__(self):
        self.told, self.sent = [], []
        self.OPENAI_API_KEY = ""
        self.bot = NS(send_chat_action=Mock())
        self._inbox = NS(destination=lambda chat: 'telegram', local=NS())

    def ensure_initialised(self, chat):
        pass

    def send_message(self, chat, text):
        self.sent.append(text)

    def _run_note_turn(self, chat, message, note):
        self.told.append(note)


class MediaPaths(unittest.TestCase):
    def test_a_voice_message_without_transcription_is_the_agents_to_explain(self):
        agent = Agent()
        SharedHandlers.handle_voice(agent, message(voice={'file_id': 'v', 'file_unique_id': 'v', 'duration': 3}))
        self.assertEqual(agent.sent, [])
        self.assertIn("could not be transcribed", agent.told[0])

    def test_a_failed_transcription_is_the_agents_to_explain(self):
        agent = Agent()
        agent.OPENAI_API_KEY = "test"
        agent.fetch_file = lambda ref, what: b"audio"
        import sys
        boom = NS(OpenAI=lambda api_key: NS(audio=NS(transcriptions=NS(create=Mock(side_effect=RuntimeError("down"))))))
        sys.modules["openai"], saved = boom, sys.modules.get("openai")
        try:
            SharedHandlers.handle_voice(agent, message(voice={'file_id': 'v', 'file_unique_id': 'v', 'duration': 3}))
        finally:
            sys.modules["openai"] = saved
        self.assertEqual(agent.sent, [])
        self.assertIn("transcribing it failed", agent.told[0])

    def test_catos_video_handler_sends_nothing_of_its_own(self):
        from tabularium.agents.coordinator import CatoApp
        agent = Agent()
        msg = message(video={'file_id': 'x', 'file_unique_id': 'x', 'width': 1, 'height': 1, 'duration': 1,
                             'file_size': video_intake.MAX_BYTES + 1})
        CatoApp.handle_video(agent, msg)                              # too big: the agent is told
        self.assertEqual(agent.told, [video_intake.TOO_BIG_NOTE])
        msg = message(2, video={'file_id': 'y', 'file_unique_id': 'y', 'width': 1, 'height': 1, 'duration': 1, 'file_size': 10})
        saved = video_intake.download
        video_intake.download = Mock(side_effect=RuntimeError("network"))
        try:
            CatoApp.handle_video(agent, msg)                          # failed: the agent is told
        finally:
            video_intake.download = saved
        self.assertIn("processing it failed", agent.told[1])
        self.assertEqual(agent.sent, [])                              # never "Processing video..." or a failure line
        agent.bot.send_chat_action.assert_called_with(OWNER_CHAT_ID, "typing")


class Inbox(unittest.TestCase):
    def test_a_file_too_big_to_download_becomes_a_turn_for_the_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            sent, told, done = [], [], threading.Event()
            inbox = DurableInbox(Path(tmp), lambda *args: sent.append(args),
                                 tell=lambda m, note: told.append((m.message_id, note)) or done.set())
            try:
                @inbox.handler
                def handle_document(m):
                    raise FileTooBig("[Alex sent big.pdf, 25.0 MB.]")
                handle_document(message(7))
                inbox.start()
                self.assertTrue(done.wait(6))
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline:
                    with inbox.db() as db:
                        if db.execute("SELECT status FROM jobs").fetchone()[0] == 'done':
                            break
                    time.sleep(0.05)
                self.assertEqual(told, [(7, "[Alex sent big.pdf, 25.0 MB.]")])
                self.assertEqual(sent, [])
            finally:
                inbox.close()


class WhenTheAgentCannotSpeak(unittest.TestCase):
    def test_a_crashed_message_is_owned_in_plain_words(self):
        with tempfile.TemporaryDirectory() as tmp:
            sent, done = [], threading.Event()
            inbox = DurableInbox(Path(tmp), lambda chat, text: sent.append(text) or done.set())
            try:
                @inbox.handler
                def handle_message(m):
                    raise RuntimeError("boom")
                handle_message(message(9))
                inbox.start()
                self.assertTrue(done.wait(6))
                self.assertEqual(len(sent), 1)
                for system_language in ("Task #", "stopped with an error", "request is saved", "replayed", "boom"):
                    self.assertNotIn(system_language, sent[0])
            finally:
                inbox.close()


if __name__ == "__main__":
    unittest.main()
