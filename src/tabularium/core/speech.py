"""Voice replies: what text is spoken, and the two services that speak it.
"""
import io
import os
import re
import requests


TTS_MAX_CHARS = 4096            # Bound the cost and length of one voice note

ELEVENLABS_TTS_MODEL = "eleven_v4"

OPENAI_TTS_MODEL = "gpt-4o-mini-tts"


class Speech:
    """Part of AgentApp (tabularium.core.agent_app): methods moved here unchanged on 2026-11-24."""

    def _tts_key(self) -> str:
        return self.OPENAI_API_KEY if self.config.tts == "openai" else os.environ.get("ELEVENLABS_API_KEY", "")

    def _tts_key_name(self) -> str:
        return "OPENAI_API_KEY" if self.config.tts == "openai" else "ELEVENLABS_API_KEY"

    def _speech_text(self, text: str) -> str:
        """What is spoken: the reply as plain text, after the name and punctuation rules every reply gets. Telegram's
        formatting is display only; until 2026-11-19 the ElevenLabs path sent it to the voice, tags and &amp; included,
        while the OpenAI path stripped it."""
        import html
        return html.unescape(re.sub(r"<[^>]+>", "", self.sanitise_for_telegram(text))).strip()

    def _speak(self, chat_id: int, text: str) -> bool:
        if self.config.tts == "openai":
            return self._speak_openai(chat_id, text)
        return self._speak_elevenlabs(chat_id, text)

    def _speak_elevenlabs(self, chat_id: int, text: str) -> bool:
        """Send ElevenLabs speech as an Ogg/Opus voice note; failure falls back to text."""
        clean = self._speech_text(text)
        key = os.environ.get("ELEVENLABS_API_KEY", "")
        if not clean or len(clean) > TTS_MAX_CHARS or not key:
            return False
        try:
            payload = {
                "text": clean, "model_id": ELEVENLABS_TTS_MODEL,
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "speed": 0.95},
            }
            chinese = bool(re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", clean))
            english = bool(re.search(r"[A-Za-z]", clean))
            if chinese and not english:
                payload["language_code"] = "zh"
            elif english and not chinese:
                payload["language_code"] = "en"
            # Mixed-language passages use the model's multilingual pronunciation.
            voice_id = os.environ.get(self.config.voice_env or "", "") or self.config.tts_voice or "c6SfcYrb2t09NHXiT80T"
            response = requests.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                headers={"xi-api-key": key},
                params={"output_format": "opus_48000_128"},
                json=payload, timeout=(10, 90),
            )
            if response.status_code != 200:
                print(f"ElevenLabs TTS failed: HTTP {response.status_code}")
                return False
            audio = response.content
            if not audio.startswith(b"OggS") or b"OpusHead" not in audio[:256]:
                print("ElevenLabs TTS failed: expected Ogg/Opus audio")
                return False
            from mutagen.oggopus import OggOpus
            import math
            seconds = OggOpus(io.BytesIO(audio)).info.length
            if not math.isfinite(seconds) or seconds <= 0:
                raise ValueError("Invalid voice duration")
            duration = max(1, math.ceil(seconds))
            buf = io.BytesIO(audio)
            buf.name = "reply.ogg"
            sent = self.bot.send_voice(chat_id, buf, duration=duration)
            reported = getattr(getattr(sent, "voice", None), "duration", None)
            print(f"[voice] audio_seconds={seconds:.2f} sent_duration={duration} telegram_duration={reported}")
            return True
        except Exception as exc:
            # Avoid logging request headers, credentials or private speech text.
            print(f"ElevenLabs TTS error: {type(exc).__name__}")
            return False

    def _speak_openai(self, chat_id: int, text: str) -> bool:
        import math
        clean = self._speech_text(text)
        if not clean or len(clean) > TTS_MAX_CHARS or not self.OPENAI_API_KEY: return False
        try:
            from openai import OpenAI
            from mutagen.oggopus import OggOpus
            response = OpenAI(api_key=self.OPENAI_API_KEY).audio.speech.create(model=OPENAI_TTS_MODEL, voice=self.config.tts_voice, input=clean, instructions=self.config.tts_instructions, response_format="opus")
            audio = response.read() if hasattr(response, "read") else response.content
            seconds = OggOpus(io.BytesIO(audio)).info.length
            if not math.isfinite(seconds) or seconds <= 0: raise ValueError("Invalid duration")
            buf = io.BytesIO(audio); buf.name = "reply.ogg"
            self.bot.send_voice(chat_id, buf, duration=max(1, math.ceil(seconds)))
            return True
        except Exception as exc:
            print(f"{self.config.display} TTS error:", type(exc).__name__)
            return False

    def tool_speak(self, text: str, chat_id: int) -> str:
        """Tool: send a spoken voice message on demand, even when Alex typed his request."""
        if not self._tts_key():
            return f"Voice unavailable — {self._tts_key_name()} is not set in .env."
        if self._speak(chat_id, text):
            return "Voice message sent."
        return "Could not send the voice message (text empty, too long, or TTS failed)."
