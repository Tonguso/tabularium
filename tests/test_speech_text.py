"""What reaches the voice (2026-11-19): the reply as plain text on both speech services. Until today the ElevenLabs
path sent Telegram's display formatting to the voice: <b> tags, &amp; for &, &lt; for <."""
import os
import unittest
from unittest.mock import patch

from tabularium.core import speech
from tabularium.core.agent_app import AgentApp
from tabularium.agents import private, coordinator, engineer


class Refused:
    status_code = 500


def spoken(config, text, env=None):
    """The URL and payload one ElevenLabs request would carry for `text` under `config`."""
    app = AgentApp.__new__(AgentApp)
    app.config = config
    sent = {}

    def post(url, json=None, **kw):
        sent.update(url=url, payload=json)
        return Refused()
    with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "test", **(env or {})}), \
            patch.object(speech.requests, "post", post):
        assert app._speak_elevenlabs(1, text) is False      # the fake service refuses; nothing is sent
    return sent["url"], sent["payload"]


class SpeechText(unittest.TestCase):
    def test_display_formatting_never_reaches_the_voice(self):
        _, payload = spoken(private.CONFIG, "**Rent** & bills, then <later>")
        self.assertEqual(payload["text"], "Rent & bills, then <later>")

    def test_code_formatting_is_not_spoken_either(self):
        _, payload = spoken(engineer.CONFIG, "Run `tests` & **check**")
        self.assertEqual(payload["text"], "Run tests & check")

    def test_chinese_reaches_the_voice_intact(self):
        _, payload = spoken(private.CONFIG, "慢慢来，不用着急。")
        self.assertEqual(payload["text"], "慢慢来，不用着急。")
        self.assertEqual(payload["language_code"], "zh")
        _, payload = spoken(private.CONFIG, "Take your time. 慢慢来。")
        self.assertNotIn("language_code", payload)            # mixed: the model pronounces each language

    def test_an_agents_voice_setting_still_overrides(self):
        url, _ = spoken(coordinator.CONFIG, "Hello", env={coordinator.CONFIG.voice_env: "chosen"})
        self.assertTrue(url.endswith("/chosen"))


if __name__ == "__main__":
    unittest.main()
