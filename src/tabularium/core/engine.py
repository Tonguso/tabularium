"""The agent runtime: one agent per process. AgentRuntime holds the agent's state, serves its inbox, sends its messages,
runs a turn on whichever route answers, and dispatches tools. Until 2026-11-23 this file was 2,500 lines and held
everything; each concern now lives in its own module and AgentRuntime inherits it unchanged: command_guard (which
commands need the owner's yes), web_tools, media_tools, research_reports, self_checks (the scheduled checks),
request_context (how a request is built from the conversation), skill_catalogue and memory_tools.
tabularium.core.shared_handlers.SharedHandlers and tabularium.core.agent_app.AgentApp extend it, and each agent's
telegram.py subclasses AgentApp.
"""
from tabularium.paths import child_env, tool_env
import base64
import io
import json
import os
import re
import subprocess
import logging
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import anthropic
import requests
import telebot

from tabularium.memory.memory_context import read_view, WorkingMemory, current_context
from tabularium.learning.learning import record as record_lesson
from tabularium.core import turn_loop
from tabularium.core.wire_budget import MAX_IMAGES, bound_result
from tabularium.core.outcome import Refused, Failed, AlreadyShown
from tabularium.core.context_window import ARCHIVE_MARKER, archive_preview, prepare_context, clear_cache_markers, wire_size, REQUEST_BYTES
from tabularium.records.background_runtime import TaskStopped
from tabularium.memory.memory_store import memory_lock, atomic_text, append_unique, reconcile, record_fact
from tabularium.records.action_evidence import verify_artifact
from tabularium.core.command_guard import CommandGuard
from tabularium.core.web_tools import WebTools
from tabularium.core.media_tools import MediaTools
from tabularium.core.research_reports import ResearchReports
from tabularium.core.self_checks import SelfChecks
from tabularium.core.request_context import RequestContext
from tabularium.core.skill_catalogue import SkillCatalogue
from tabularium.core.memory_tools import MemoryTools


class RunawayStop(RuntimeError):
    """Raised by execute_tool after RUNAWAY_STREAK consecutive failed or refused tool results: the model is
    looping and must stop with a diagnosis rather than buy more turns (task 82, 2026-10-07: ten identical
    rejections, five budget extensions, 4.9M tokens, nothing delivered)."""


class _ChatGPTQuotaReached(Exception):
    pass


from tabularium.core.shared_handlers import SharedHandlers   # noqa: E402  (after the module's own names, which it does not need)


class AgentRuntime(CommandGuard, WebTools, MediaTools, ResearchReports, SelfChecks, RequestContext, SkillCatalogue, MemoryTools, SharedHandlers):
    """One agent. Constants are class attributes (shared, never mutated); per-agent state is set in __init__.
    Until 2026-10-14 ~100 module globals were copied onto each instance by a hand-kept STATE_NAMES list."""

    OPEN_THREADS_MAX    = 10
    THREAD_STALE_DAYS   = 7
    ANTHROPIC_MODEL_DEFAULT = "claude-sonnet-5"
    OPENAI_MODEL_DEFAULT    = "gpt-6.1-sol"
    GEMINI_MODEL_DEFAULT    = "gemini-4-carbon"         # the API name; the CLI adds an effort suffix
    CHATGPT_PLAN_MODEL      = "gpt-6.1-sol"
    ANTHROPIC_MODEL_ALIASES = {
        # Short names → latest of that family
        "haiku":      "claude-haiku-4-5",
        "sonnet":     "claude-sonnet-5-5",
        "opus":       "claude-opus-5-5",
        "fable":      "claude-fable-5-1",
        # Version-specific
        "haiku 4.5":  "claude-haiku-4-5",
        "sonnet 5":   "claude-sonnet-5",
        "sonnet 5.5": "claude-sonnet-5-5",
        "sonnet 4.6": "claude-sonnet-4-6",
        "opus 4.6":   "claude-opus-4-6",
        "opus 4.7":   "claude-opus-4-7",
        "opus 4.8":   "claude-opus-4-8",
        "opus 5":     "claude-opus-5",
        "opus 5.5":   "claude-opus-5-5",
        "fable 5":    "claude-fable-5",
        "fable 5.1":  "claude-fable-5-1",
    }
    _TIER_ORDER  = ["haiku", "sonnet", "opus", "fable"]
    _TIER_LATEST = {"haiku": "claude-haiku-4-5", "sonnet": "claude-sonnet-5-5",
                    "opus": "claude-opus-5-5",  "fable": "claude-fable-5-1"}
    EMAIL_TOOLS_ANTHROPIC = [
        {
            "name": "read_email",
            "description": (
                "Read this agent's own mailbox (an AgentMail inbox; the address is in every result). Newest first. "
                "Lists show a preview; pass message_id to read one message in full."),
            "input_schema": {
                "type": "object",
                "properties": {
                    "count":      {"type": "integer", "description": "Number of messages to list (default 10, max 50)."},
                    "sender":     {"type": "string",  "description": "Only messages whose From contains this (optional)."},
                    "keyword":    {"type": "string",  "description": "Only messages whose subject contains this (optional)."},
                    "message_id": {"type": "string",  "description": "Read this one message in full (from a listing)."},
                },
                "required": [],
            },
        },
        {
            "name": "send_email",
            "description": "Send an email from this agent's own mailbox. Always confirm to/subject/body with Alex before calling this. The body is Markdown-shaped text (paragraphs, **bold**, - lists, # headings) and goes out as HTML with a plain-text copy. Result is a receipt: confirmed_sent means the service accepted the message, not that the recipient has it.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "to":          {"type": "string", "description": "Recipient address(es), semicolon-separated."},
                    "subject":     {"type": "string", "description": "Email subject."},
                    "body":        {"type": "string", "description": "Email body as Markdown-shaped text."},
                    "cc":          {"type": "string", "description": "CC address(es), semicolon-separated (optional)."},
                    "attachments": {"type": "string", "description": "Optional file path(s) to attach, semicolon-separated; 6 MB in all. Corporate mail filters often quarantine .py and raw .html; rename code to .txt or zip it before attaching."},
                    "reply_to_id": {"type": "string", "description": "message_id of a received message to reply to; the mail threads under it and the subject is theirs (optional)."},
                    "resend":      {"type": "boolean", "description": "Only true if Alex explicitly requests a duplicate email. Never use to recover from a timeout."},
                },
                "required": ["to", "subject", "body"],
            },
        },
    ]
    _NAME_FIX = re.compile(r"亚历克斯")
    _CJK_CH = r"一-鿿㐀-䶿"
    _FULLWIDTH = {",": "，", ".": "。", ":": "：", ";": "；", "?": "？", "!": "！",
                  "(": "（", ")": "）"}
    _PUNCT_NEAR_CJK = re.compile(
        rf"(?<=[{_CJK_CH}])\s*([,.:;?!()])"          # Chinese char before it
        rf"|([,.:;?!()])(?=\s*[{_CJK_CH}])"          # or Chinese char after it
    )
    _MAX_TG = 4000
    _CJK = re.compile(r'[一-鿿]')
    LATE_MESSAGE_SECONDS = 120
    STT_MODEL = "gpt-transcribe"
    _STT_JUNK = (
        "请不吝点赞", "订阅 转发 打赏", "明镜与点点栏目", "字幕由", "字幕志愿者",
        "Subtitles by", "Amara.org", "感谢观看", "谢谢观看", "请点赞订阅",
    )
    VOICE_REPLY_HINT = (
        "\n\n[You are replying to a voice message. Set modality to voice if the reply is conversational and works "
        "heard aloud; set it to text if it contains anything better seen than heard — formulas, code, tables, lists, "
        "precise figures, or links. For voice, follow your 'Speaking Aloud' rules — short, contractions, fragments, "
        "varied rhythm, no lists or structure, rounded numbers. You are talking, not reading.]"
    )
    ADVISOR_SYSTEM = (
        "You are a rigorous senior advisor giving a second opinion on another assistant's plan or "
        "work before it acts. Be critical, specific, and concise. Point out errors, flawed "
        "assumptions, missing steps, and risks, and say exactly what is wrong and why. If the plan "
        "or answer is sound, say so plainly rather than inventing objections. Your job is to catch "
        "mistakes, not to rewrite everything. Separate facts, assumptions, inference and preferences. "
        "Assess both sides of a disagreement against supplied evidence; confidence and seniority are not evidence. "
        "Say what would change your conclusion and disclose what you cannot verify. Do not invent sources."
    )
    VIEW_IMAGE_MAX_EDGE = 1568
    TOOL_RESULT_HISTORY_CAP = 16000
    _REPEAT_LIMIT = 2
    RUNAWAY_STREAK = 5
    MAX_TOOL_ITERATIONS = 60
    TOOL_BUDGET_WARN_AT = 42
    OVERLOAD_FALLBACK_MODELS = ["claude-opus-5-5", "claude-sonnet-5", "claude-haiku-4-5"]
    WORKER_REQUEST_BYTES = 160_000
    # Inside a turn the request only grows, so its prefix stays cached; past this ceiling it is cut once to 60% of it
    # and grows again from there. Well above working size (task 232's largest request was about 540 KB), far below
    # where a provider refuses (2026-11-19: from 2026-11-15 nothing bounded a turn between its start and that refusal).
    STEP_CEILING_BYTES = 600_000
    MAX_IMAGES_PER_TURN = MAX_IMAGES
    REPLY_QUOTE_CHARS = 400
    RunawayStop = RunawayStop
    _ChatGPTQuotaReached = _ChatGPTQuotaReached

    def __init__(self):
        """Per-agent state. Constants are class attributes above; one agent runs per process. The keys are read here,
        when the agent is built, not when the module is imported: the process loads its .env first (2026-11-08)."""
        self.ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
        self.OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
        self.GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")        # the Gemini API route (2026-11-15)
        self.TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
        self.ANYSEARCH_API_KEY = os.environ.get("ANYSEARCH_API_KEY", "")
        self.anthropic_client = anthropic.Anthropic(api_key=self.ANTHROPIC_API_KEY, timeout=600.0, max_retries=2)
        self._draining = threading.Event()
        self.conversations = {}
        self._turns_since_activity = {}
        self._abort_requested = {}
        self.model_providers = {}
        self._chat_locks = {}
        self._approving = None          # (tool, inputs) of the parked action Alex just approved, while it runs
        self.openai_models = {}
        self.anthropic_models = {}
        self._last_language_hint = "\n\n[Reply in 简体中文 (never 繁體) unless Alex writes in English.]"
        self.voice_modes = {}
        self._review_counts = {}
        self.auto_modes = {}
        self._last_photo = {}
        self._playwright_instance = None
        self._browser_instance = None
        self._page_instance = None
        self._browser_lock = threading.Lock()
        self._browser_attached = False
        self._repeat_calls = {}
        self._unproductive_streak = {}
        self._window_anchor = {}
        self._context_sizes = {}
        self._turn_notes = {}
        self._completion_feedback = {}
        self._truncations = {}
        self._core_versions = {}
        self._episode_reads = {}

    def drain_flag(self):
        return self.AGENT_DIR / "task_state" / "drain"

    def wait_for_quiet(self, inbox, timeout=3600, poll=2):
        """Block until the durable inbox has no pending or running job (True), or the timeout passes (False)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with inbox.db() as db:
                busy = db.execute("SELECT count(*) FROM jobs WHERE status IN ('pending','running')").fetchone()[0]
            store = getattr(self, '_background', None)
            if store is not None:
                from tabularium.records.worker_lease import busy as lease_busy
                busy += sum(proc.poll() is None for proc in store.processes.values())
                busy += sum(lease_busy(path) for path in (store.agent_dir/'task_state/background').glob('*/worker.lock'))
            if not busy:
                return True
            time.sleep(poll)
        return False


    def serve_forever(self):
        """Poll Telegram until task_state/drain appears, then stop taking updates, finish queued work and exit 0.
        The supervisor starts the process again on the current code: that is how a restart works (2026-10-09).
        Updates not yet fetched stay with Telegram and reach the new process."""
        from tabularium.ops import release
        if not release.same_source(release.identity(), release.LOADED):
            raise RuntimeError('Source changed during agent startup')
        flag = self.drain_flag()
        from tabularium.ops.supervisor import read_json, RESTART_EXIT
        request = {}

        def watch():
            nonlocal request
            while True:
                request = read_json(flag)
                if request.get('id') and request.get('instance') == self._inbox.health.get('instance'):
                    break
                time.sleep(2)
            print("[drain] restart requested; no new messages will be taken")
            self._draining.set()
            try:
                self.bot.stop_polling()
            except Exception:
                pass
        threading.Thread(target=watch, daemon=True, name="drain-watch").start()
        if os.environ.get("AGENT_SELFTEST", "1") != "0":
            threading.Thread(target=self._selftest_worker, daemon=True, name="selftest").start()
        # Self-healing polling: if the machine sleeps or the network drops and polling exits or
        # crashes, restart it with a fresh session instead of letting the process go silent.
        while not self._draining.is_set():
            try:
                self.bot.infinity_polling(logger_level=logging.CRITICAL, timeout=20, long_polling_timeout=20)
                if not self._draining.is_set():
                    print("Polling stopped unexpectedly; restarting in 5s...")
            except KeyboardInterrupt:
                return
            except Exception as e:
                print(f"Polling crashed ({type(e).__name__}); restarting in 5s...")
            if self._draining.is_set():
                break
            try:
                telebot.apihelper._get_req_session(reset=True)  # drop a poisoned connection pool (e.g. after sleep)
            except Exception:
                pass
            time.sleep(5)
        paused = self._background.pause_running() if getattr(self, '_background', None) else []
        if paused:
            print(f"[drain] pausing {len(paused)} worker(s) at the next safe point; they resume on the new build")
        finished = self.wait_for_quiet(self._inbox)
        while not finished:
            print('[drain] active work still owns its slot; continuing to wait')
            finished = self.wait_for_quiet(self._inbox)
        self._inbox.progress('restart_ready', restart=request)
        print('[drain] work finished; exiting for restart')
        sys.stdout.flush()
        os._exit(RESTART_EXIT)

    def _claim_single_instance(self, port: int, label: str) -> None:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", port))
            s.listen(1)
        except OSError:
            print(f"{label} is already running (port {port} held) — exiting so the two don't "
                  f"fight over the Telegram token.")
            raise SystemExit(0)
        self._INSTANCE_LOCK = s

    def get_chat_lock(self, chat_id: int) -> threading.Lock:
        if chat_id not in self._chat_locks:
            self._chat_locks[chat_id] = threading.Lock()
        return self._chat_locks[chat_id]

    def get_openai_model(self, chat_id: int) -> str:
        if self._routing.model("openai"):
            return self._routing.model("openai")
        return self.openai_models.get(chat_id, self.OPENAI_MODEL_DEFAULT)

    def advisor_model_for(self, model: str) -> str:
        tier = next((t for t in self._TIER_ORDER if t in model), "sonnet")
        above = self._TIER_ORDER[min(self._TIER_ORDER.index(tier) + 1, len(self._TIER_ORDER) - 1)]
        return self._TIER_LATEST[above]

    def get_provider(self, chat_id: int) -> str:
        if self._routing.provider():
            return self._routing.provider()
        return self.model_providers.get(chat_id, self.DEFAULT_MODEL_PROVIDER)

    def get_anthropic_model(self, chat_id: int) -> str:
        if self._routing.model("anthropic"):
            return self._routing.model("anthropic")
        return self.anthropic_models.get(chat_id, self.ANTHROPIC_MODEL_DEFAULT)

    def _model_prefs_path(self, chat_id: int) -> Path:
        return self.AGENT_DIR / f"model_{chat_id}.json"

    def _fix_chinese_text(self, text: str) -> str:
        text = self._NAME_FIX.sub("Alex", text)
        # Only punctuation touching a Chinese char converts, so decimals (3.5), times (9:30),
        # paths (D:\agents) and plain English are all left alone.
        return self._PUNCT_NEAR_CJK.sub(lambda m: self._FULLWIDTH[m.group(1) or m.group(2)], text)

    def reply_target(self, chat_id: int, message_id=None, reply=None):
        """The message a reply should quote, or None. A background result quotes its request. A turn's reply quotes
        the latest of the messages it declares it answers (`answers`), and only when Alex has written since; a
        reply that answers his latest message is not quoted (2026-10-21: three messages in one turn, the reply
        answered the third and quoted the first)."""
        inbox = getattr(self, "_inbox", None)
        if not message_id and inbox is not None and hasattr(inbox, "turn_messages"):
            from tabularium.core.reply_envelope import answered
            turn = inbox.turn_messages()
            hits = answered(reply, turn) if reply is not None else []
            message_id = (hits or turn or [(None, "")])[-1][0]
        if not message_id and inbox is not None and hasattr(inbox, "answering"):
            message_id = inbox.answering()
        if message_id and inbox and hasattr(inbox, "superseded") and inbox.superseded(chat_id, message_id):
            return message_id
        return None

    def send_message(self, chat_id: int, text: str, reply_to=None) -> bool:
        source = reply_to
        inbox = getattr(self, '_inbox', None)
        if source is None and inbox is not None:
            from tabularium.core.reply_envelope import answered
            turn = inbox.turn_messages()
            hits = answered(text, turn)
            source = (hits or turn or [(inbox.answering(), '')])[-1][0]
        channel = inbox.destination(chat_id, source) if inbox is not None and hasattr(inbox, 'destination') else 'telegram'
        decision = getattr(text, 'decision_id', 0)
        job = getattr(getattr(inbox, 'local', None), 'job', None)        # the turn this message belongs to
        if channel == 'dashboard':
            self.outbox().put(chat_id, [str(text)], decision=decision, channel=channel, original=str(text), job=job)
            return True
        reply_to = self.reply_target(chat_id, reply_to, text)
        # A Telegram reply can follow a dashboard request; never quote its local reference on Telegram.
        if reply_to and reply_to < 0:
            reply_to = None
        decision = getattr(text, "decision_id", 0)
        clean = self.sanitise_for_telegram(text or "")
        chunks = []
        while len(clean) > self._MAX_TG:
            split_at = clean.rfind('\n', 0, self._MAX_TG)
            if split_at <= 0:
                split_at = self._MAX_TG
            chunks.append(clean[:split_at])
            clean = clean[split_at:].lstrip('\n')
        chunks.append(clean)
        # Durable until Telegram accepts it (tabularium.records.outbox, 2026-10-21): a network failure queues the message
        # for the retry thread instead of dropping it, so True means "accepted for delivery".
        self.outbox().put(chat_id, chunks, reply_to, decision, original=str(text), job=job, buttons=getattr(text, 'buttons', False))
        return True

    def outbox(self):
        if getattr(self, "_outbox", None) is None:
            from tabularium.records.outbox import Outbox
            def reset():
                telebot.apihelper._get_req_session(reset=True)      # same reset the polling loop uses after a crash
            self._outbox = Outbox(self.RUNTIME_DIR, lambda: self.bot, self._decision_asked, reset)
        return self._outbox

    def _decision_asked(self, decision, message_id):
        """The Telegram id of a message that asked a decision: Alex's reply to it answers the decision."""
        if getattr(self, "_background", None) is not None:
            from tabularium.records.task_decisions import bind_messages
            bind_messages(self._background, decision, [message_id])

    def _clean_transcript(self, text: str) -> str:
        """Strip transcriptions that are pure model hallucination rather than speech."""
        t = (text or "").strip()
        if not t:
            return ""
        if any(j in t for j in self._STT_JUNK):
            print(f"[stt] Discarded hallucinated transcript: {t[:80]!r}")
            return ""
        return t

    def _voice_mode_path(self, chat_id: int) -> Path:
        return self.AGENT_DIR / f"voice_{chat_id}.json"

    def get_voice_mode(self, chat_id: int) -> str:
        if chat_id not in self.voice_modes:
            mode = "auto"
            p = self._voice_mode_path(chat_id)
            if p.exists():
                try:
                    mode = json.loads(p.read_text(encoding="utf-8")).get("mode", "auto")
                except Exception:
                    mode = "auto"
            self.voice_modes[chat_id] = mode
        return self.voice_modes[chat_id]

    def set_voice_mode(self, chat_id: int, mode: str) -> None:
        self.voice_modes[chat_id] = mode
        try:
            self._voice_mode_path(chat_id).write_text(json.dumps({"mode": mode}), encoding="utf-8")
        except OSError as e:
            print(f"[state] voice mode not saved for chat {chat_id}: {e}")

    def maybe_speak(self, chat_id: int, text: str, from_voice: bool) -> None:
        """Speak the reply when appropriate: always in 'on', never in 'off',
        only for voice-originated turns in 'auto' (mirror)."""
        if self._inbox.destination(chat_id) == "dashboard":
            return
        mode = self.get_voice_mode(chat_id)
        if mode == "off":
            return
        if mode == "auto" and not from_voice:
            return
        self._speak(chat_id, text)

    def tool_consult_advisor(self, request: str, chat_id: int) -> str:
        advisor = self.advisor_model_for(self.get_anthropic_model(chat_id) if self.get_provider(chat_id) == "anthropic" else self.get_openai_model(chat_id))
        try:
            self._inbox.record_event('attempt', advisor, 'api', 'anthropic', role='advisor')
            resp = self.anthropic_client.messages.create(
                model=advisor,
                max_tokens=2048,
                system=self.ADVISOR_SYSTEM,
                messages=[{"role": "user", "content": request}],
            )
            u = getattr(resp, "usage", None)
            self._inbox.record_event("response", advisor, "api", "anthropic", u, role="advisor")
            if u is not None:
                print(f"[usage-advisor] {advisor}  in={getattr(u, 'input_tokens', 0)}  "
                      f"out={getattr(u, 'output_tokens', 0)}")
            parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
            return f"ADVISOR ({advisor}):\n" + ("\n".join(parts).strip() or "(no response)")
        except Exception as e:
            return f"Advisor unavailable: {e}"

    def tool_specialist_review(self, role, question, evidence, chat_id, proposed_answer="", counterevidence="", delivery_id=""):
        from tabularium.learning.specialist_review import prompt
        try:
            system, packet = prompt(self.config.display, role, question, evidence, proposed_answer, counterevidence)
        except ValueError as exc:
            return str(exc)
        count = self._review_counts.get(chat_id, 0)
        if count >= 3:
            return "Reviewer budget reached: three calls this turn. Resolve from evidence or report uncertainty."
        self._review_counts[chat_id] = count + 1
        primary = self.get_anthropic_model(chat_id) if self.get_provider(chat_id) == "anthropic" else self.get_openai_model(chat_id)
        model = self.advisor_model_for(primary)
        path = self.AGENT_DIR / "reports" / "specialist_reviews" / (str(time.time_ns()) + ".json")
        record = {"role": role, "model": model, "chat": chat_id, "packet": json.loads(packet), "delivery_id": delivery_id,
                  "status": "started", "independence": "fresh context; model and evidence-selection biases remain"}
        from tabularium.core.agent_runtime import atomic_json
        atomic_json(path, record)
        try:
            self._inbox.record_event("attempt", model, "api", "anthropic", role="reviewer")
            response = self.anthropic_client.with_options(timeout=120.0, max_retries=0).messages.create(
                model=model, max_tokens=2500, system=system,
                messages=[{"role":"user","content":packet}])
            usage = getattr(response, "usage", None)
            self._inbox.record_event("response", model, "api", "anthropic", usage, role="reviewer")
            answer = "\n".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()
            record.update(status="finished" if answer else "empty", answer=answer)
            atomic_json(path, record)
            return json.dumps({"role":role,"model":model,"review":answer or "No review returned.",
                               "record":str(path),"warning":"Advisory only; no external checks or actions performed."},ensure_ascii=False)
        except Exception as exc:
            record.update(status="error",error=type(exc).__name__)
            atomic_json(path,record)
            return "Specialist unavailable (" + type(exc).__name__ + "). No review completed. Record: " + str(path)

    def tool_read_email(self, count: int = 10, sender: str = "", keyword: str = "", message_id: str = "") -> str:
        try:
            if message_id:
                m = self._mail.message(message_id)
                text = m.get("text") or m.get("extracted_text") or re.sub(r"<[^>]+>", " ", m.get("html") or "")
                return json.dumps({"message_id": m.get("message_id"), "thread_id": m.get("thread_id"), "from": m.get("from"),
                                   "to": m.get("to"), "cc": m.get("cc"), "date": m.get("timestamp"), "subject": m.get("subject"),
                                   "attachments": [a.get("filename") for a in m.get("attachments") or []],
                                   "text": text[:20000]}, ensure_ascii=False)
            rows = self._mail.messages(limit=min(count or 10, 50), sender=sender, subject=keyword)
            where = self._mail.address()
            if not rows:
                return f"No mail in {where}" + (" matching the filters." if sender or keyword else ".")
            return f"{len(rows)} message(s) in {where}, newest first:\n\n" + "\n\n---\n\n".join(
                f"id: {m.get('message_id')}\nFrom: {m.get('from', '')}\nTo: {', '.join(m.get('to') or [])}\n"
                f"Date: {m.get('timestamp', '')}\nSubject: {m.get('subject', '')}\nPreview: {m.get('preview', '')}"
                + (f"\nAttachments: {', '.join(a.get('filename', '') for a in m['attachments'])}" if m.get("attachments") else "")
                for m in rows)
        except Exception as exc:
            return Failed(f"Error reading email: {exc}")

    def tool_send_email(self, to: str, subject: str, body: str, cc: str = "", attachments: str = "",
                        reply_to_id: str = "", resend: bool = False) -> str:
        try:
            payload = dict(to=to, subject=subject, body=body, cc=cc, reply_to_id=reply_to_id, from_address=self._mail.address(),
                           attachments=[p.strip().strip('"') for p in attachments.split(";") if p.strip()])
        except Exception as exc:
            return Failed(f"Mailbox unavailable: {exc}")
        return json.dumps(self._email_receipts.send(self._mail, payload, resend), ensure_ascii=False)

    def tool_email_status(self, receipt_id: str) -> str:
        return json.dumps(self._email_receipts.get(receipt_id), ensure_ascii=False)


    def tool_write_file(self, path: str, content: str) -> str:
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.resolve() == self.MEMORY_FILE.resolve():
                return "Use your memory tool (remember, or append/reconcile where those exist); do not overwrite the memory file."
            atomic_text(p, content)
            return json.dumps(verify_artifact(str(p)), ensure_ascii=False)
        except Exception as e:
            return Failed(f"Error writing file: {e}")

    def tool_edit_file(self, path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
        """Exact-match find/replace. Exists because write_file on a 28 KB mockup blew through the
        16k output cap thirteen times in three days and delivered nothing."""
        try:
            p = Path(path)
            if not p.is_file():
                return Failed(f"Error: {path} does not exist. Use write_file to create a new file.")
            if not old_string:
                return Failed("Error: old_string is empty.")
            if p.resolve() == self.MEMORY_FILE.resolve():
                if replace_all:
                    return "Memory changes need an exact unique old value; use the memory tool's old/new correction."
                return reconcile(self.MEMORY_FILE, old_string, new_string, "Updated through edit_file")
            text = p.read_text(encoding="utf-8")
            n = text.count(old_string)
            if n == 0:
                return Failed("Error: old_string not found. Check whitespace and indentation exactly, "
                        "or read_file to see the current content.")
            if n > 1 and not replace_all:
                return Failed(f"Error: old_string occurs {n} times. Include more surrounding context so it "
                              f"is unique, or set replace_all to true.")
            new_text = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
            atomic_text(p, new_text)
            line = text[:text.index(old_string)].count("\n") + 1
            return (f"Edited {path}: replaced {n if replace_all else 1} occurrence(s) "
                    f"(first at line {line}); file is now {len(new_text)} characters.")
        except Exception as e:
            return Failed(f"Error editing file: {e}")


    def _cap_result(self, result: str, limit: int = TOOL_RESULT_HISTORY_CAP) -> str:
        """Keep the response bounded, with the exact original available for paged retrieval."""
        if len(result) <= limit:
            return result
        return archive_preview(result, self.AGENT_DIR / "context_archive" / "results", limit - 1200)

    def execute_tool(self, name: str, inputs: dict, chat_id: int = None):
        """Refuse the third identical call in a row that would get the identical result. On 2026-10-07 a
        worker sent the same invalid review_output ten times, got the same rejection every time, and paid
        ~160k tokens a turn for it. Identical inputs and identical result twice running means the model is
        looping, not working; the refusal tells it to change something or report the blocker."""
        import hashlib
        key = name + "\x00" + json.dumps(inputs, sort_keys=True, ensure_ascii=False, default=str)
        history = self._repeat_calls.setdefault(chat_id, [])
        recent = history[-self._REPEAT_LIMIT:]
        approved = bool(getattr(self, '_approving', None))        # the owner's yes runs a parked call whatever came before it
        if not approved and len(recent) == self._REPEAT_LIMIT and all(k == key for k, _ in recent) and len({d for _, d in recent}) == 1:
            return self._note_outcome(name, Refused(
                f"NOT executed: this exact {name} call has already run {self._REPEAT_LIMIT} times in a row with the identical "
                "result. Repeating it cannot change the outcome. Read the previous result, change the inputs or the tool, "
                "or report the blocker and stop."), chat_id)
        try:
            result = self._execute_tool_unguarded(name, inputs, chat_id)
            result = bound_result(result, self.AGENT_DIR / "context_archive" / "results")
            if name == "read_file":
                result = self._dedup_episode_read(chat_id, inputs, result)
            elif name == "skill":
                result = self._dedup_episode_skill(chat_id, inputs, result)
        except TaskStopped:
            raise                      # worker control flow, not a tool failure
        except Exception as exc:
            # A tool's own failure is feedback for the model, never a worker crash: on 2026-10-08 13:42 a ValueError
            # from task_checkpoint ended task 94's worker and Alex got "stopped before I could finish".
            result = Failed(f"Error: {name} failed: {type(exc).__name__}: {str(exc)[:400]}")
        # Hash the tool's own output, not the per-turn budget line feedback() appends: its numbers change on
        # every call, so with them included two identical failures never looked identical (Cato's review,
        # 2026-10-07 21:12: the guard was silently defeated in every background worker).
        core = str(result).split("\n\n[Budget ", 1)[0]
        history.append((key, hashlib.sha256(core.encode("utf-8", "replace")).hexdigest()))
        del history[:-self._REPEAT_LIMIT]
        return self._note_outcome(name, result, chat_id)

    def _dedup_episode_read(self, chat_id, inputs, result):
        """Within one episode, an unchanged file span already shown above returns a compact reference instead of
        injecting a second copy into the prompt."""
        import hashlib
        from tabularium.learning.trials import overridden
        if not isinstance(inputs, dict) or "path" not in inputs or self.unproductive(result) or not isinstance(result, str):
            return result
        if result.startswith(ARCHIVE_MARKER):
            return result
        try:
            raw = overridden(inputs["path"]) or inputs["path"]
            p = Path(raw)
            if not p.is_file():
                return result
            data = p.read_bytes()
            text_len = len(data.decode("utf-8"))
            start = int(inputs.get("offset", 0) or 0)
            limit = int(inputs.get("limit", 8000) if inputs.get("limit") is not None else 8000)
            end = min(start + limit, text_len)
            resolved = str(p.resolve())
        except (OSError, UnicodeDecodeError, ValueError, TypeError):
            return result
        digest = hashlib.sha256(data).hexdigest()
        state = self._episode_state(chat_id)
        key = (resolved, digest)
        for seen_start, seen_end, read_no in state["spans"].get(key, ()):
            if seen_start <= start and end <= seen_end:
                return AlreadyShown(f"[Unchanged since read #{read_no} in this session "
                                    f"(characters {start}:{end} of {text_len}); use the text already shown above.]")
        state["seq"] += 1
        state["spans"].setdefault(key, []).append((start, end, state["seq"]))
        return result

    def _episode_state(self, chat_id):
        return self._episode_reads.setdefault(chat_id, {"seq": 0, "spans": {}, "skills": {}})


    def unproductive(self, result):
        """True for a refusal or a failure: a result the model cannot build on. The kind is in the type (tabularium.core.outcome)."""
        return isinstance(result, (Refused, Failed))

    def _note_outcome(self, name, result, chat_id):
        failed = self.unproductive(result)
        streak = self._unproductive_streak.get(chat_id, 0) + 1 if failed else 0
        self._unproductive_streak[chat_id] = streak
        if streak >= self.RUNAWAY_STREAK:
            self._unproductive_streak[chat_id] = 0
            self._inbox.record_event("bounce", "runaway", role="runtime")
            raise self.RunawayStop(f"{streak} consecutive tool calls failed or were refused; the last was {name} -> "
                              f"{str(result)[:160]}. Stopped rather than spend more on a loop. Work so far is saved; "
                              "a resume needs a different approach, not a retry.")
        return result


    def run_turn_openai(self, messages: list, chat_id: int) -> str:
        return turn_loop.run(self, messages, chat_id, turn_loop.OpenAIResponses(self, chat_id))

    def run_turn_gemini(self, messages: list, chat_id: int) -> str:
        from tabularium.models.gemini_api import GeminiGenerate          # the key route, no harness (2026-11-15)
        return turn_loop.run(self, messages, chat_id, GeminiGenerate(self, chat_id))

    def run_turn(self, messages: list, chat_id: int) -> str:
        # The loop guards count within one turn: a failure in Alex's last request must not carry a
        # streak into his next one, and a resumed worker starts clean.
        self._completion_feedback.pop(chat_id, None)
        self._repeat_calls.pop(chat_id, None)
        self._unproductive_streak.pop(chat_id, None)
        self._episode_reads.pop(chat_id, None)
        from tabularium.core.session_lifecycle import continue_turn
        result = continue_turn(self, messages, chat_id, lambda: self._routing.run(self, messages, chat_id))
        if self.BACKGROUND_TASK_ID or self._rehearsing:
            return result
        corrected = getattr(result, "correction", ())
        for ident in getattr(result, "not_correction", ()):   # the judgement on strong language is recorded either way
            try:
                self._inbox.record_event('considered', model='owner', role='foreground', details={'message_id': int(ident), 'verdict': 'not a correction'})
            except (OSError, ValueError, AttributeError) as exc:
                print(f"[correction] not recorded: {type(exc).__name__}: {exc}")
        from tabularium.records.task_decisions import verify_reply
        result = verify_reply(self, messages, chat_id, result)
        if corrected:
            from tabularium.learning import corrections
            try:
                corrections.start(self, chat_id, corrected)
            except (ValueError, OSError) as exc:     # the reply still goes out; the correction is logged as missed
                print(f"[correction] not recorded: {type(exc).__name__}: {exc}")
        return result


    def save_chat_id(self, chat_id: int) -> None:
        try:
            self.CHAT_ID_FILE.write_text(str(chat_id), encoding="utf-8")
        except OSError as e:
            print(f"[state] chat id not saved: {e}")

    def ensure_initialised(self, chat_id: int) -> None:
        self.save_chat_id(chat_id)
        if chat_id not in self.conversations:
            self.load_model_prefs(chat_id)
            self.conversations[chat_id] = self.load_conversation(chat_id)
            note = self._inbox.recovery_note(chat_id)
            if note:
                self.conversations[chat_id].append({'role': 'user', 'content': '[Local recovery record]\n' + note})
                self.save_conversation(chat_id)


    def _check_task_waiting(self, chat_id):
        if self.BACKGROUND_TASK_ID:
            decisions = self._background.decisions(chat_id, int(self.BACKGROUND_TASK_ID))
            if decisions:
                from tabularium.core.session_lifecycle import TaskWaiting
                raise TaskWaiting(decisions[0])

    def _background_boundary(self, messages, api_messages, chat_id):
        """What arrived while this turn was running, delivered at a tool boundary: a steer to a background task, or a
        follow-up message from Alex to a foreground turn (2026-10-16: two bubbles are one thought, not two tasks).
        Returns the entries delivered now. The API routes send api_messages on the next call; the Claude subscription
        runs one live session per turn, so its tool handler hands these to the model inside the tool result."""
        fresh = []
        if self.BACKGROUND_TASK_ID:
            ident = int(self.BACKGROUND_TASK_ID)
            if self._background.get(ident)["status"] in ("cancelled", "cancelling", "pausing"):
                raise TaskStopped()
            updates = self._background.pending_controls(ident)
            if updates:
                # Save before acknowledging; control IDs avoid replay after a crash.
                for update in updates:
                    tag = f"[Background scope update #{update['id']}]"
                    if any(tag in str(m.get("content", "")) for m in messages):
                        continue
                    entry = {"role": "user", "content": tag + "\n" + update["text"]}
                    messages.append(entry)
                    api_messages.append(entry)
                    fresh.append(entry)
                self.save_conversation(chat_id)
                self._background.acknowledge([u["id"] for u in updates])
            self._check_task_waiting(chat_id)
            return fresh
        job = getattr(getattr(self._inbox, "local", None), "job", None)
        if job:
            taken = []
            for ident, message in self._inbox.follow_ups(chat_id, job, presses=True):
                # A verdict on this turn's own ask runs the parked action now; the outcome joins the turn
                # (2026-11-15 14:41: a desktop grant's "Yes" arrived as text and the model could only ask again).
                content = self._settle_follow_up(chat_id, message) or self.compose_turn(message, nudge=False)
                if not isinstance(content, str):          # a reply to a photo stays its own turn
                    break
                entry = {"role": "user", "content": content}
                messages.append(entry)
                api_messages.append(entry)
                fresh.append(entry)
                taken.append(ident)
                local = getattr(self._inbox, "local", None)
                if local is not None and getattr(message, "channel", "") != "button":   # a tap is carried out, not answered
                    local.turn_messages = list(getattr(local, "turn_messages", None) or []) + [(message.message_id, message.text)]
            if taken:
                self.save_conversation(chat_id)           # saved before the jobs are marked done, never lost
                self._inbox.absorb(taken)
        return fresh

    def tool_background_task(self, action: str, chat_id: int, task_id: int = 0, request: str = "", context: str = "", retry_mode: str = "", completion_items=None) -> str:
        # retry_mode is accepted and ignored: the read_only mode was removed on 2026-10-07; old prompts may still pass it.
        if action == "status":
            return json.dumps(self._background.overview(chat_id), ensure_ascii=False)
        if self.BACKGROUND_TASK_ID:
            return "Already in a background task. Execute here; do not spawn or control other tasks."
        try:
            if action == "start":
                # The complete original conversation remains accessible, without bloating the worker prompt.
                context += "\nOriginal chat file (read relevant portions only if needed): " + str(self._conversation_path(chat_id))
                result = self._background.start(chat_id, request, context, completion_items=completion_items)
            else:
                result = self._background.control(chat_id, task_id, action, request)
        except ValueError as exc:
            # The store refuses bad requests with ValueError (unknown task, already
            # active ...). Returned as the tool result so the model can correct the call; raised, it ended the
            # whole foreground turn with "Task stopped" (2026-10-07 20:53).
            return f"Not started: {exc}"
        return json.dumps(result, ensure_ascii=False)

    def detect_media_type(self, data: bytes) -> str:
        if data[:3] == b'\xff\xd8\xff':
            return "image/jpeg"
        if data[:8] == b'\x89PNG\r\n\x1a\n':
            return "image/png"
        if data[:6] in (b'GIF87a', b'GIF89a'):
            return "image/gif"
        if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
            return "image/webp"
        return "image/jpeg"

    def _run_turn_with_guard(self, messages: list, chat_id: int) -> str | None:
        self._refresh_core_memory(messages, chat_id)

        # Persist incoming requests before any model/tool call. Failure stops execution.
        self._recall.record(chat_id, self._serialise_messages(messages), self._conversation_path(chat_id))
        from tabularium.core.context_window import exchange_starts
        starts = exchange_starts(messages)
        if len(starts) > 2 * self.WINDOW_HIGH:
            messages[:] = messages[:2] + messages[starts[-self.WINDOW_HIGH]:]
        self.save_conversation(chat_id)
        try:
            self._inbox.local.tool_ids = set()
            repaired = self.recover_tool_history(messages, self.RUNTIME_DIR)
            messages[:] = repaired
            return self.run_turn(messages, chat_id)
        except Exception as e:
            self._inbox.fail_current()
            # A failed request must not remove a user message or a completed tool result.
            # The agent cannot answer, so the runtime does, in plain words; the cause goes to the log and the task record.
            print(f'[turn] stopped: {type(e).__name__}: {e}')
            self.send_message(chat_id, self._stopped_note(messages))
            return None
        finally:
            self._abort_requested.pop(chat_id, None)
            self.save_conversation(chat_id)

    def _stopped_note(self, messages) -> str:
        """What the runtime says when the agent could not run, in the language Alex just wrote in.

        2026-11-19: a Chinese conversation got this in English and read as the agent ignoring the question."""
        text = ''
        for entry in reversed(messages or []):
            if entry.get('role') == 'user':
                content = entry.get('content')
                text = content if isinstance(content, str) else ' '.join(
                    b.get('text', '') for b in content if isinstance(b, dict)) if isinstance(content, list) else ''
                break
        from tabularium.core.agent_runtime import runtime_words
        return runtime_words('stopped', text)

    def _run_image_turn(self, chat_id: int, images: list, caption: str | None, sent=None) -> None:
        """One turn carrying any number of images."""
        lock = self.get_chat_lock(chat_id)
        lock.acquire()
        try:
            content = [{"type": "image",
                        "source": {"type": "base64", "media_type": mt, "data": b64}}
                       for mt, b64 in images]
            if caption:
                note = caption
            elif len(images) > 1:
                note = f"[Alex sent {len(images)} photos — treat them as one set]"
            else:
                note = "[Alex sent a photo]"
            originals = []
            for media_type, encoded in images:
                saved = self._images.store(chat_id, base64.b64decode(encoded), media_type, caption or "",
                                      getattr(sent, "date", sent) if isinstance(getattr(sent, "date", sent), (int,float)) else None)
                originals.append(saved["path"])
            content.append({"type": "text", "text": f"{self._ts(sent)} {note}\nSaved originals: " + "; ".join(originals)})

            messages = self.conversations[chat_id]
            messages.append({"role": "user", "content": content})
            self.save_conversation(chat_id)
            if self._inbox.destination(chat_id) != 'dashboard':
                self.bot.send_chat_action(chat_id, "typing")
            reply = self._run_turn_with_guard(messages, chat_id)
            if reply is None:
                return
            self.save_conversation(chat_id)
            self.send_message(chat_id, reply)
            self.maybe_speak(chat_id, reply, from_voice=False)
        finally:
            lock.release()

    def _run_note_turn(self, chat_id: int, message, note: str) -> None:
        """A turn about something Alex sent: `note` says what he sent and what became of it (a file's text, or why a
        video, file or voice message could not be read), and the agent answers in its own words. The runtime never
        writes that reply itself; until 2026-11-19 it sent "Processing video...", "File type '.foo' not supported" or
        "Voice error: ..." in system language beside the agent's voice."""
        caption = getattr(message, "caption", None) or ""
        content = f"{self._ts(message)} {self._reply_context(message, with_image=False)[0]}{note}"
        if caption:
            content += f"\n\nAlex's note: {caption}"
        lock = self.get_chat_lock(chat_id)
        lock.acquire()
        try:
            messages = self.conversations[chat_id]
            messages.append({"role": "user", "content": content})
            self.save_conversation(chat_id)
            if self._inbox.destination(chat_id) != 'dashboard':
                self.bot.send_chat_action(chat_id, "typing")
            reply = self._run_turn_with_guard(messages, chat_id)
            if reply is None:
                return
            self.save_conversation(chat_id)
            self.send_message(chat_id, reply)
            self.maybe_speak(chat_id, reply, from_voice=False)
        finally:
            lock.release()

    def _reply_context(self, message, with_image: bool = True):
        """(note, image_block) describing the message Alex is replying to, or ("", None)."""
        ref = getattr(message, "reply_to_message", None)
        if ref is None:
            return "", None
        try:
            who = "your" if getattr(getattr(ref, "from_user", None), "is_bot", False) else "his own"
            when = datetime.fromtimestamp(ref.date)
            stamp = when.strftime("%H:%M") if when.date() == datetime.now().date() else when.strftime("%Y-%m-%d %H:%M")
            quote = getattr(message, "quote", None)
            quoted = getattr(quote, "text", None) if quote else None
            body = " ".join((quoted or ref.text or ref.caption or "").split())
            if len(body) > self.REPLY_QUOTE_CHARS:
                body = body[:self.REPLY_QUOTE_CHARS].rstrip() + "…"
            kind = ("photo" if ref.photo else "voice note" if ref.voice else
                    f"file {ref.document.file_name}" if ref.document else "")
            if body:
                part = "the highlighted part of " if quoted else ""
                note = f'[Alex is replying to {part}{who} message from {stamp}: {"[" + kind + "] " if kind else ""}"{body}"'
            else:
                note = (f"[Alex is replying to {who} {kind or 'message'} from {stamp}; its text is not in the "
                        f"reply, use search_conversations around that time if you need it")
            image = None
            if with_image and ref.photo:
                info = self.bot.get_file(ref.photo[-1].file_id)
                data = self.bot.download_file(info.file_path)
                media_type, encoded, _ = self._image_for_model(data)
                image = {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": encoded}}
                note += "; that photo is attached again"
            return note + "]\n", image
        except Exception as e:                      # never lose Alex's message over reply metadata
            print(f"[reply-context] {type(e).__name__}: {e}")
            return "", None


    def _recover_recent_photos(self):
        try:
            from tabularium.channels.owner_telegram import OWNER_CHAT_ID
            recovered = self._images.recover_telegram(self.bot, OWNER_CHAT_ID)
            print(f"[image-memory] Recovered {recovered} earlier Telegram photo(s).")
        except Exception as exc:
            print(f"[image-memory] Recovery unavailable: {type(exc).__name__}")


    def run_turn_anthropic(self, messages: list, chat_id: int) -> str:
        return turn_loop.run(self, messages, chat_id, turn_loop.AnthropicMessages(self, chat_id))

    def _cache_key(self):
        """One key per conversation: OpenAI routes requests with the same prompt_cache_key to the same cache node.
        Measured 2026-10-08: 19% cache reads over 4M input tokens on the ChatGPT route without it."""
        return f"{self.AGENT_DIR.name}-{self.BACKGROUND_TASK_ID or 'chat'}"

