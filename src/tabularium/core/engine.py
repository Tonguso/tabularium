"""The agent runtime: model loops for every route, tool execution and its guards, request building (history window,
current memory, turn notes), memory and recovery. One class, AgentRuntime; tabularium.core.shared_handlers.SharedHandlers
and tabularium.core.agent_app.AgentApp extend it, and each agent's telegram.py subclasses AgentApp. One agent per process.
"""
from tabularium.paths import child_env
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
from tabularium.core import taint
from tabularium.core.wire_budget import MAX_IMAGES, bound_result
from tabularium.core.outcome import Refused, Failed
from tabularium.core.context_window import archive_preview, prepare_context, clear_cache_markers, wire_size, REQUEST_BYTES
from tabularium.records.background_runtime import TaskStopped
from tabularium.memory.memory_store import memory_lock, atomic_text, append_unique, reconcile, record_fact
from tabularium.records.action_evidence import verify_artifact


class RunawayStop(RuntimeError):
    """Raised by execute_tool after RUNAWAY_STREAK consecutive failed or refused tool results: the model is
    looping and must stop with a diagnosis rather than buy more turns (task 82, 2026-10-07: ten identical
    rejections, five budget extensions, 4.9M tokens, nothing delivered)."""


class _ChatGPTQuotaReached(Exception):
    pass


from tabularium.core.shared_handlers import SharedHandlers   # noqa: E402  (after the module's own names, which it does not need)



def mark_image(data, marks, crop=None):
    """The picture with the question drawn on it: red boxes with labels in the original file's pixels, then an
    optional crop (2026-11-08: a shape question goes out as a marked picture, not as a numbered list of geometry)."""
    from PIL import Image, ImageDraw, ImageFont
    image = Image.open(io.BytesIO(data)).convert("RGB")
    draw = ImageDraw.Draw(image)
    stroke = max(2, min(image.size) // 300)
    try:
        font = ImageFont.load_default(size=max(14, min(image.size) // 40))
    except (TypeError, OSError):
        font = ImageFont.load_default()
    for m in marks:
        x, y, w, h = int(m["x"]), int(m["y"]), int(m["w"]), int(m["h"])
        draw.rectangle([x, y, x + w, y + h], outline=(230, 30, 30), width=stroke)
        label = str(m.get("label") or "").strip()
        if label:
            left, top, right, bottom = draw.textbbox((0, 0), label, font=font)
            tw, th = right - left, bottom - top
            ly = y - th - 2 * stroke - 4 if y - th - 2 * stroke - 4 >= 0 else y + h + stroke
            draw.rectangle([x, ly, x + tw + 2 * stroke + 4, ly + th + 2 * stroke + 4], fill=(230, 30, 30))
            draw.text((x + stroke + 2 - left, ly + stroke + 2 - top), label, fill="white", font=font)
    if crop:
        x, y, w, h = (int(v) for v in crop)
        image = image.crop((max(0, x), max(0, y), min(image.width, x + w), min(image.height, y + h)))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()

class AgentRuntime(SharedHandlers):
    """One agent. Constants are class attributes (shared, never mutated); per-agent state is set in __init__.
    Until 2026-10-14 ~100 module globals were copied onto each instance by a hand-kept STATE_NAMES list."""

    OPEN_THREADS_MAX    = 10
    THREAD_STALE_DAYS   = 7
    ANTHROPIC_MODEL_DEFAULT = "claude-sonnet-5"
    OPENAI_MODEL_DEFAULT    = "gpt-6.1-sol"
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
    OUTLOOK_TOOLS_ANTHROPIC = [
        {
            "name": "list_email_accounts",
            "description": "List all Outlook email accounts available to send from.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "read_email",
            "description": (
                "Read emails from Alex's Outlook. Searches ALL connected accounts by default "
                "(owner@example.com, owner@example.com, work@example.com) and merges "
                "them newest-first, so you never need to know which mailbox something landed in. "
                "Each result is labelled with the account it came from."),
            "input_schema": {
                "type": "object",
                "properties": {
                    "count":   {"type": "integer", "description": "Number of emails to retrieve (default 10, max 50)."},
                    "sender":  {"type": "string",  "description": "Filter by sender name or email address (optional)."},
                    "keyword": {"type": "string",  "description": "Filter by keyword in subject or body (optional)."},
                    "folder":  {"type": "string",  "description": "Folder to read from: inbox, sent, drafts (default: inbox)."},
                    "account": {"type": "string",  "description": "Restrict to one mailbox, e.g. 'owner'. Omit to search every account (the normal case)."},
                },
                "required": [],
            },
        },
        {
            "name": "send_email",
            "description": "Send an email via Alex's Outlook. Always confirm with Alex before calling this. If Alex specifies a sender address, you MUST pass it as from_address — never omit it. Result is a receipt: submitted is not confirmed_sent, and neither proves recipient delivery.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "to":           {"type": "string", "description": "Recipient email address(es), semicolon-separated."},
                    "subject":      {"type": "string", "description": "Email subject."},
                    "body":         {"type": "string", "description": "Email body (plain text)."},
                    "cc":           {"type": "string", "description": "CC email address(es), semicolon-separated (optional)."},
                    "from_address": {"type": "string", "description": "Sender email address. REQUIRED when Alex specifies which account to send from. Must exactly match an account from list_email_accounts."},
                    "attachments":  {"type": "string", "description": "Optional file path(s) to attach, semicolon-separated. e.g. 'D:\\\\work\\\\notes.txt' or 'D:\\\\a.txt;D:\\\\b.txt'. Corporate mail filters often quarantine .py and raw .html; rename code to .txt or zip it before attaching."},
                    "resend":       {"type": "boolean", "description": "Only true if Alex explicitly requests a duplicate email. Never use to recover from a timeout."},
                },
                "required": ["to", "subject", "body"],
            },
        },
    ]
    BROWSER_TOOLS_ANTHROPIC = [
        {
            "name": "browser_close",
            "description": "Close the browser and free all associated resources. Use when done with browser tasks.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "browser_navigate",
            "description": (
                "Open a URL in a headless Chromium browser and return the page's text content. "
                "Browser state persists across tool calls — you can navigate, then click, then type "
                "across multiple calls without losing context. Always navigate before screenshot/click/type."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "url":      {"type": "string", "description": "Full URL to navigate to (include https://)."},
                    "authenticated": {"type": "boolean", "description": "Compatibility option. Both values use the normal saved-login browser; no paywall extension is loaded."},
                    "wait_for": {"type": "string", "description": "Wait condition: 'load' (default), 'networkidle', 'domcontentloaded'."},
                },
                "required": ["url"],
            },
        },
        {
            "name": "browser_screenshot",
            "description": "Take a screenshot of the current browser page and send it to Alex on Telegram. Use after browser_navigate to show Alex what the page looks like.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "browser_click",
            "description": "Click an element on the current browser page. Returns the page text after the click. Useful for buttons, links, tabs.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector or Playwright text selector, e.g. 'button#submit', 'text=Accept', '.nav-link'."},
                },
                "required": ["selector"],
            },
        },
        {
            "name": "browser_type",
            "description": "Type text into an input field on the current browser page. Clears any existing value first.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector of the input field."},
                    "text":     {"type": "string", "description": "Text to type into the field."},
                },
                "required": ["selector", "text"],
            },
        },
    ]
    TAVILY_SEARCH_TOOL_ANTHROPIC = {
        "name": "web_search",
        "description": "Search the web using Tavily.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "The search query."}},
            "required": ["query"],
        },
    }
    TAVILY_SEARCH_TOOL_OPENAI = {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web using Tavily.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "The search query."}},
                "required": ["query"],
            },
        },
    }
    CACHE_CONTROL = {"type": "ephemeral", "ttl": "1h"}
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
    _NO_OUTLOOK_PROFILE_MSG = (
        "Outlook has no mail account set up on this machine, so I can't read or send email. "
        "That empty setup is why an Outlook 'add account' window keeps popping up. "
        "Alex needs to open Outlook (classic) once and add his account (e.g. owner@example.com); "
        "after that I'll be able to use it. I won't keep opening the window in the meantime."
    )
    EMAIL_SCAN_LIMIT = 400
    _CATASTROPHIC_CMD = re.compile(
        r'(?i)('
        r'\bformat-volume\b|\bdiskpart\b'
        r'|\bstop-computer\b|\brestart-computer\b|\bshutdown\b'
        r'|\buninstall-|\breg\s+delete\b'
        r')'
    )
    _DELETION_CMD = re.compile(
        r'(?i)(\bremove-item\b|\bdel\b|\berase\b|\brmdir\b|\brd\b|\brm\b)'
    )
    _MASS_DELETE = re.compile(r'(?i)(-recurse\b|/s\b|\*)')
    _DISPOSABLE_DIR = re.compile(r'(?i)\\(?:scratch|temp|tmp|__pycache__|\.pytest_cache|\.ruff_cache|\.mypy_cache)(?:\\|$)'
                                 r'|^(?:\$env:temp|%temp%)')
    _PATH_LITERAL = re.compile(r'''(?i)(?:[a-z]:\\|\$env:temp|%temp%)[^'"\s;|,)}]*''')
    _VAR_ASSIGN = re.compile(r'''\$(\w+)\s*=\s*['"]([^'"]+)['"]''')
    _VAR_REF = re.compile(r'''\$(\w+)([^\s'";|,)}]*)''')
    _NEUTRAL_VARS = {"_", "true", "false", "null", "env", "psitem"}
    _CD = re.compile(r'''(?i)^\s*(?:cd|chdir|set-location|sl|push-location|pushd)\s+(?:-(?:literal)?path\s+)?['"]?([^'";|]+?)['"]?\s*$''')
    _ARG_TOKEN = re.compile(r'''"[^"]*"|'[^']*'|[^\s,]+''')
    _TARGET_FLAGS = {"-path", "-literalpath"}
    _VALUE_FLAGS = {"-filter", "-include", "-exclude", "-erroraction", "-ea"}
    _PROCESS_KILL = re.compile(
        r'(?i)(\bstop-process\b|\bspps\b|\btaskkill\b|\bpkill\b|\|\s*kill\b|\bkill\s+(?=-|\d|\$))'
    )
    _PID_ONLY_KILL = re.compile(
        r'(?i)^\s*(?:stop-process|spps|kill)\s+(?:-id\s+)?\d+(?:\s*,\s*\d+)*'
        r'(?:\s+-(?:force|f|confirm:\$false|erroraction\s+\w+|ea\s+\w+))*\s*$'
        r'|^\s*taskkill(?:\s+/[ft])*\s+/pid\s+\d+(?:\s+/[ft])*\s*$'
    )
    KILL_REFUSAL = Refused("Refused, not executed: process kills must name literal PIDs (Stop-Process -Id N, "
                           "taskkill /PID N). A by-name/filter kill stopped all four agents on 2026-10-06.")
    COMMAND_TIMEOUT = 60
    OUTLOOK_TIMEOUT = 120
    VIEW_IMAGE_MAX_EDGE = 1568
    IMAGE_MODEL = "gpt-image-2"
    ZSXQ_DEFAULT_URL  = "https://wx.zsxq.com/group/YOUR_GROUP_ID"
    ANYSEARCH_URL = "https://api.anysearch.com/v1/search"
    TOOL_RESULT_HISTORY_CAP = 16000
    _REPEAT_LIMIT = 2
    RUNAWAY_STREAK = 5
    _BLOCK_FIELDS = {
        "text":              ("text",),
        "tool_use":          ("id", "name", "input"),
        "tool_result":       ("tool_use_id", "content", "is_error"),
        "thinking":          ("thinking", "signature"),
        "redacted_thinking": ("data",),
        "image":             ("source",),
    }
    MAX_TOOL_ITERATIONS = 60
    TOOL_BUDGET_WARN_AT = 42
    OVERLOAD_FALLBACK_MODELS = ["claude-opus-5-5", "claude-sonnet-5", "claude-haiku-4-5"]
    WORKER_REQUEST_BYTES = 160_000
    ALWAYS_SKILLS: tuple = ()
    MAX_IMAGES_PER_TURN = MAX_IMAGES
    REPLY_QUOTE_CHARS = 400
    RunawayStop = RunawayStop
    _ChatGPTQuotaReached = _ChatGPTQuotaReached

    def __init__(self):
        """Per-agent state. Constants are class attributes above; one agent runs per process. The keys are read here,
        when the agent is built, not when the module is imported: the process loads its .env first (2026-11-08)."""
        self.ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
        self.OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
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
        self._tainted = {}                 # chat -> the reader that brought external content into this turn (tabularium.core.taint)
        self._core_versions = {}

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

    SELFTEST_SECONDS = 24 * 3600
    SELFTEST_FIRST_DELAY = 300

    WEEKLY_SECONDS = 7 * 24 * 3600
    WEEKLY_CHECKS = (("memory_review", "run_memory_review"), ("memory_exam", "run_memory_exam"),
                     ("lesson_digest", "run_lesson_digest"))                                       # json name, method

    def _selftest_worker(self):
        """Daily self-test, plus the weekly memory review and memory exam (2026-10-11). A weekly check runs when its
        task_state/<name>.json is older than a week or missing, or when task_state/<name>_requested exists
        (python agent_control.py review|exam)."""
        time.sleep(self.SELFTEST_FIRST_DELAY)
        last_selftest = 0
        while True:
            if time.time() - last_selftest >= self.SELFTEST_SECONDS:
                last_selftest = time.time()
                try:
                    self.run_selftest()
                except Exception as exc:
                    print(f"[selftest] worker error: {type(exc).__name__}: {exc}")
            for name, method in self.WEEKLY_CHECKS:
                flag = self.AGENT_DIR / "task_state" / f"{name}_requested"
                record = self.AGENT_DIR / "task_state" / f"{name}.json"
                due = flag.exists() or not record.exists() or time.time() - record.stat().st_mtime >= self.WEEKLY_SECONDS
                if not due:
                    continue
                flag.unlink(missing_ok=True)
                try:
                    getattr(self, method)()
                except Exception as exc:
                    print(f"[{name}] worker error: {type(exc).__name__}: {exc}")
            try:
                self.run_benchmark_rerun()                                       # a model or route change is rescored (2026-11-02)
            except Exception as exc:
                print(f"[benchmark_rerun] worker error: {type(exc).__name__}: {exc}")
            try:
                if self._cli_changed():                                          # a Claude update: prove the turn still works
                    last_selftest = time.time()
                    self.run_selftest()
            except Exception as exc:
                print(f"[selftest] update check error: {type(exc).__name__}: {exc}")
            try:
                from tabularium.learning import corrections   # a quiet thread of corrections: one diagnosis
                corrections.flush(self)
            except Exception as exc:
                print(f"[correction] flush error: {type(exc).__name__}: {exc}")
            flag = self.AGENT_DIR / "task_state" / "memory_trial_requested"     # consolidation trial (2026-10-20)
            if flag.exists():
                flag.unlink(missing_ok=True)
                try:
                    self.run_memory_trial()
                except Exception as exc:
                    print(f"[memory_trial] worker error: {type(exc).__name__}: {exc}")
            time.sleep(60)

    def run_benchmark_rerun(self):
        """A model or route change reruns the benchmark, once per route (2026-11-02, the review article: rescore
        whenever the model changes; until now a digest line only said a rerun was due). The run is the Sunday
        profile in its own process, logging to logs/eval-<agent>.log; the dashboard's Regression eval row then shows
        the new score against the previous route's."""
        from tabularium.learning import regression
        from tabularium.ops.operations import read_json
        from tabularium.core.agent_runtime import atomic_json
        current = regression.primary_route(self.AGENT_DIR)
        if not current or not regression.route_gap(self.AGENT_DIR):
            return False
        record = self.AGENT_DIR / "task_state" / "benchmark_rerun.json"
        if read_json(record).get("route") == current:
            return False                                                         # launched for this route already
        root = self.AGENT_DIR.parent
        with open(root / "logs" / f"eval-{self.AGENT_DIR.name}.log", "ab") as log:   # the child keeps its own handle
            subprocess.Popen([sys.executable, "-m", "tabularium", "eval", self.AGENT_DIR.name, "--k", "3", "--sample", "8"],
                             cwd=str(root), env=child_env(), stdout=log, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        atomic_json(record, {"route": current, "time": time.time()})
        print(f"[benchmark_rerun] primary route is now {current}: benchmark rerun launched")
        return True

    def run_lesson_digest(self):
        """Weekly: the skill edits awaiting Alex's go, sent by the runtime, so a proposal never depends on the agent
        remembering to show it (2026-11-01: 40 proposals had sat unseen; Alex: [quote removed]). The dashboard lists the same edits at any time."""
        from tabularium.channels.owner_telegram import OWNER_CHAT_ID
        from tabularium.core.agent_runtime import atomic_json
        from tabularium.learning import learning
        text = learning.awaiting_go(self.AGENT_DIR, OWNER_CHAT_ID) if self.config.role.learns else ""
        sent = bool(text) and self.send_message(OWNER_CHAT_ID, text)
        atomic_json(self.AGENT_DIR / "task_state" / "lesson_digest.json", {"time": time.time(), "sent": bool(sent), "chars": len(text)})
        return text

    def _cli_changed(self):
        """True once per change of the Claude Code CLI the subscription route would use (a new version after an
        update, or none at all). 2026-10-20: an update moved the CLI and the agents ran on the fallback for two
        days; the daily self-test passed because the fallback answered. Now the path is checked every minute and a
        change runs the self-test at once, with the route it used in the record."""
        from tabularium.models.subscription_runtime import find_cli
        from tabularium.core.agent_runtime import atomic_json
        path = self.AGENT_DIR / "task_state" / "claude_cli.json"
        current = find_cli() or ""
        try:
            previous = json.loads(path.read_text(encoding="utf-8")).get("path", "")
        except (OSError, ValueError):
            previous = None
        if previous == current:
            return False
        atomic_json(path, {"path": current, "changed": time.time()})
        if previous is None:
            return False                                 # first record on this installation: nothing changed
        print(f"[selftest] Claude CLI changed: {previous or '(none)'} -> {current or '(none)'}; running the self-test now")
        return True

    def run_selftest(self):
        """One real turn, composed the way the message handler composes it (compose_turn on a synthetic Telegram
        message) and run through run_turn: the model must call read_file on the identity file and answer OK.
        Result in task_state/selftest.json; the supervisor tells Alex on failure, the dashboard shows it.
        Runs under the chat lock so it never interleaves with a real conversation turn (2026-10-11). Nothing is sent
        to Telegram. Before 2026-10-11 18:00 this bypassed the handler and passed while every real message failed."""
        from tabularium.channels.owner_telegram import OWNER_CHAT_ID
        from tabularium.core.agent_runtime import atomic_json
        from types import SimpleNamespace
        path = self.AGENT_DIR / "task_state" / "selftest.json"
        target = self.AGENT_DIR / "SKILL.md"
        text = ("[Runtime self-test, not a message from Alex; send nothing to Telegram and change nothing.] "
                f"Call read_file on {target} (a few lines are enough), then reply with exactly the word OK.")
        incoming = SimpleNamespace(chat=SimpleNamespace(id=OWNER_CHAT_ID), text=text, date=int(time.time()), reply_to_message=None)
        messages = []
        started = time.time(); reply = ""; error = ""; ok = False
        self.ensure_initialised(OWNER_CHAT_ID)
        with self.get_chat_lock(OWNER_CHAT_ID):
            try:
                messages.append({"role": "user", "content": self.compose_turn(incoming, nudge=False)})
                reply = self.run_turn(messages, OWNER_CHAT_ID) or ""
                used_tool = any(isinstance(m.get("content"), list) and any(isinstance(b, dict) and b.get("type") == "tool_use" for b in m["content"])
                               for m in messages)
                ok = "OK" in reply.upper() and used_tool
                if not ok:
                    error = "no tool call" if not used_tool else "reply was not OK"
            except Exception as exc:
                error = f"{type(exc).__name__}: {str(exc)[:300]}"
        record = {"time": time.time(), "ok": ok, "seconds": round(time.time() - started, 1), "reply": reply[:200], "error": error}
        # Which route answered, and whether it was the first choice: for two days (2026-10-20 to 10-22) the daily
        # self-test passed on the ChatGPT fallback while the Claude CLI was missing, and nothing said so.
        try:
            state = json.loads((self.AGENT_DIR / "task_state" / "routing_state.json").read_text(encoding="utf-8"))
            routes = self._routing.config(self.AGENT_DIR).get("routes") or []
            route = state.get("route") or {}
            record["route"] = route.get("kind", "")
            record["fallback"] = bool(routes and route and route.get("kind") != routes[0].get("kind"))
            if record["fallback"]:
                print(f"[selftest] passed on the fallback route {route.get('kind')}; first choice {routes[0].get('kind')} did not answer")
        except (OSError, ValueError, AttributeError, KeyError):
            pass
        atomic_json(path, record)
        print(f"[selftest] {'passed' if ok else 'FAILED'} in {record['seconds']}s" + (f": {error}" if error else ""))
        return record

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
        if channel == 'dashboard':
            self.outbox().put(chat_id, [str(text)], decision=decision, channel=channel, original=str(text))
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
        self.outbox().put(chat_id, chunks, reply_to, decision, original=str(text))
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

    def _outlook_has_profile(self) -> bool:
        """True if a configured Outlook/MAPI mail profile exists. Checking first avoids
        triggering Outlook's account-setup wizard when there is no profile."""
        try:
            import winreg
            keys = [
                r"Software\Microsoft\Office\16.0\Outlook\Profiles",
                r"Software\Microsoft\Windows NT\CurrentVersion\Windows Messaging Subsystem\Profiles",
            ]
            for path in keys:
                try:
                    k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
                    has_sub = winreg.QueryInfoKey(k)[0] > 0
                    winreg.CloseKey(k)
                    if has_sub:
                        return True
                except FileNotFoundError:
                    continue
            return False
        except Exception:
            return True

    def tool_list_email_accounts(self) -> str:
        if not self.OUTLOOK_AVAILABLE:
            return "Outlook COM not available."
        if not self._outlook_has_profile():
            return self._NO_OUTLOOK_PROFILE_MSG
        try:
            self.pythoncom.CoInitialize()
            outlook = self.win32.Dispatch("Outlook.Application")
            accounts = outlook.Session.Accounts
            result = []
            for i in range(1, accounts.Count + 1):
                acc = accounts.Item(i)
                result.append(f"{i}. {acc.DisplayName} <{acc.SmtpAddress}>")
            return "\n".join(result) if result else "No accounts found."
        except Exception as e:
            return Failed(f"Error listing accounts: {e}")
        finally:
            self.pythoncom.CoUninitialize()

    def tool_read_email(self, count: int = 10, sender: str = "", keyword: str = "",
                        folder: str = "inbox", account: str = "") -> str:
        if not self.OUTLOOK_AVAILABLE:
            return "Outlook COM not available."
        if not self._outlook_has_profile():
            return self._NO_OUTLOOK_PROFILE_MSG
        try:
            self.pythoncom.CoInitialize()
            outlook = self.win32.Dispatch("Outlook.Application")
            ns = outlook.GetNamespace("MAPI")

            folder_name = {"inbox": "Inbox", "sent": "Sent Items", "drafts": "Drafts"}.get(
                folder.lower(), "Inbox")

            # Search EVERY connected account, not just the default delivery store.
            # ns.GetDefaultFolder() opens only the profile's default store — here that is
            # owner@example.com, a low-traffic mailbox whose newest mail is months old.
            # Reading it returned real but useless results that looked like stale data.
            count = min(count or 10, 50)
            s_low, k_low = sender.lower(), keyword.lower()
            want_account = (account or "").lower()
            found, scanned_stores, skipped = [], [], []

            for si in range(1, ns.Folders.Count + 1):
                store = ns.Folders.Item(si)
                store_name = str(store.Name)
                if want_account and want_account not in store_name.lower():
                    continue
                try:
                    mapi_folder = store.Folders.Item(folder_name)
                except Exception:
                    skipped.append(f"{store_name} (no {folder_name})")
                    continue

                items = mapi_folder.Items
                try:
                    items.Sort("[ReceivedTime]", True)      # newest first
                except Exception:
                    pass
                scanned_stores.append(f"{store_name}:{items.Count}")

                # Walk newest-first and stop early: these mailboxes run to thousands of items
                # and every property read is a COM round trip.
                hits, seen = 0, 0
                for i in range(1, items.Count + 1):
                    if hits >= count or seen >= self.EMAIL_SCAN_LIMIT:
                        break
                    seen += 1
                    try:
                        item = items.Item(i)
                        subject = str(getattr(item, "Subject", "") or "")
                        sender_name = str(getattr(item, "SenderName", "") or "")
                        sender_addr = str(getattr(item, "SenderEmailAddress", "") or "")
                        if sender and s_low not in sender_name.lower() and s_low not in sender_addr.lower():
                            continue
                        # Body is the expensive read, so only fetch it once cheap filters pass.
                        body = str(getattr(item, "Body", "") or "")
                        if keyword and k_low not in subject.lower() and k_low not in body.lower():
                            continue
                        received = getattr(item, "ReceivedTime", None)
                        stamp = received.strftime("%Y-%m-%d %H:%M") if received else "unknown"
                        sort_key = received.timestamp() if received else 0.0

                        preview = body.strip()[:500].replace("\r\n", " ").replace("\n", " ")
                        if len(body.strip()) > 500:
                            preview += "..."
                        found.append((sort_key,
                                      f"Account: {store_name}\n"
                                      f"From: {sender_name} <{sender_addr}>\n"
                                      f"Date: {stamp}\n"
                                      f"Subject: {subject}\n"
                                      f"Body: {preview}"))
                        hits += 1
                    except Exception:
                        continue    # meeting requests and similar have no ReceivedTime

            found.sort(key=lambda r: r[0], reverse=True)     # merge the accounts by date
            results = [txt for _, txt in found[:count]]

            where = f" across {len(scanned_stores)} account(s) [{', '.join(scanned_stores)}]"
            if not results:
                return (f"No emails found matching the criteria{where}."
                        + (f" Skipped: {', '.join(skipped)}." if skipped else ""))
            return (f"Retrieved {len(results)} email(s){where}:\n\n"
                    + "\n\n---\n\n".join(results))

        except Exception as e:
            return Failed(f"Error reading email: {e}")
        finally:
            self.pythoncom.CoUninitialize()

    def tool_send_email(self, to: str, subject: str, body: str, cc: str = "", from_address: str = "",
                        attachments: str = "", resend: bool = False) -> str:
        if not self.OUTLOOK_AVAILABLE or not self._outlook_has_profile():
            return self._NO_OUTLOOK_PROFILE_MSG
        self.pythoncom.CoInitialize()
        try:
            payload = dict(to=to, subject=subject, body=body, cc=cc,
                           from_address=from_address.strip() or "owner@example.com",
                           attachments=[p.strip().strip('"') for p in attachments.split(";") if p.strip()])
            receipt = self._email_receipts.send(self.win32.Dispatch("Outlook.Application"), payload, resend)
            return json.dumps(receipt, ensure_ascii=False)
        except Exception as exc:
            return f"Email outcome unverified ({type(exc).__name__}). Inspect receipts/Outbox/Sent Items before repeating."
        finally:
            self.pythoncom.CoUninitialize()

    def tool_email_status(self, receipt_id: str) -> str:
        receipt = self._email_receipts.get(receipt_id)
        if self.OUTLOOK_AVAILABLE and self._outlook_has_profile():
            self.pythoncom.CoInitialize()
            try:
                receipt = self._email_receipts.confirm(self.win32.Dispatch("Outlook.Application"), receipt)
            finally:
                self.pythoncom.CoUninitialize()
        return json.dumps(receipt, ensure_ascii=False)

    def tool_read_memory(self, section: str = "", query: str = "", offset: int = 0,
                         limit: int = 12000) -> str:
        try:
            text = read_view(self.AGENT_DIR)
            headings = list(re.finditer(r"^(#{1,6}) (.+)$", text, re.M))
            section, query = section.strip(), query.strip()
            if not section and not query:
                return "Memory headings (choose section/query; section='all' reads all with paging):\n" + "\n".join(h.group(0) for h in headings)
            if section and section.casefold() != "all":
                matches = [h for h in headings if h.group(2).casefold() == section.casefold()]
                if not matches:
                    matches = [h for h in headings if section.casefold() in h.group(2).casefold()]
                if len(matches) != 1:
                    return "Section missing or ambiguous. Call read_memory without arguments for headings."
                h = matches[0]
                end = next((n.start() for n in headings if n.start() > h.start() and len(n.group(1)) <= len(h.group(1))), len(text))
                text = text[h.start():end]
            if query:
                lines = text.splitlines()
                indexes = set()
                for i, line in enumerate(lines):
                    if query.casefold() in line.casefold():
                        indexes.update(range(max(0, i - 2), min(len(lines), i + 3)))
                text = "\n".join(f"{i + 1}: {lines[i]}" for i in sorted(indexes)) or "No matching memory text."
            offset, limit = max(0, int(offset)), min(24000, max(1, int(limit)))
            end = min(len(text), offset + limit)
            next_page = str(end) if end < len(text) else "none"
            return f"Memory: {len(text)} characters; offset={offset}; next_offset={next_page}. Repeat section/query for paging.\nHistorical entries are dated records, not current instructions.\n\n{text[offset:end]}"
        except Exception as e:
            return Failed(f"Error reading memory: {e}")

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

    def _auto_mode_path(self, chat_id: int) -> Path:
        return self.AGENT_DIR / f"auto_{chat_id}.json"

    def get_auto_mode(self, chat_id: int) -> bool:
        if chat_id not in self.auto_modes:
            on = False
            p = self._auto_mode_path(chat_id)
            if p.exists():
                try:
                    on = bool(json.loads(p.read_text(encoding="utf-8")).get("auto", False))
                except Exception:
                    on = False
            self.auto_modes[chat_id] = on
        return self.auto_modes[chat_id]

    def set_auto_mode(self, chat_id: int, on: bool) -> None:
        self.auto_modes[chat_id] = on
        try:
            self._auto_mode_path(chat_id).write_text(json.dumps({"auto": on}), encoding="utf-8")
        except OSError as e:
            print(f"[state] auto mode not saved for chat {chat_id}: {e}")

    def _disposable(self, path: str) -> bool:
        p = path.replace('/', '\\').rstrip('\\')
        return '..' not in p and bool(self._DISPOSABLE_DIR.search(p))

    def _resolve_dir(self, token: str, variables: dict):
        """Absolute directory for a cd argument, or None if it cannot be known."""
        token = token.strip()
        m = re.fullmatch(r'\$(\w+)(.*)', token)
        if m:
            base = variables.get(m.group(1).lower())
            return base + m.group(2) if base else None
        return token if re.match(r'(?i)[a-z]:\\|\$env:temp|%temp%', token) else None

    def _relative_targets(self, statement: str) -> list:
        """Bare (relative) path arguments given to the delete verb itself or, when the delete
        takes its input from a pipe ("Get-ChildItem x | Remove-Item"), to the command feeding it."""
        verb = self._DELETION_CMD.search(statement)
        own = self._bare_args(re.split(r'[|})]', statement[verb.end():], maxsplit=1)[0])
        if own or '|' not in statement[:verb.start()]:
            return own
        source = statement[:verb.start()].split('|')[0].strip().lstrip('{(')
        return self._bare_args(source.split(None, 1)[1] if len(source.split(None, 1)) > 1 else '')

    def _bare_args(self, rest: str) -> list:
        out, expect = [], None                   # expect: "target" or "value" for the next token
        for tok in self._ARG_TOKEN.findall(rest):
            tok = tok.strip('\'"')
            if expect == "value":                # e.g. SilentlyContinue after -ErrorAction
                expect = None
                continue
            if expect == "target":
                expect = None
                if tok and not tok.startswith(('$', '-')) and not self._PATH_LITERAL.match(tok):
                    out.append(tok)
                continue
            low = tok.lower()
            if low in self._TARGET_FLAGS:
                expect = "target"
            elif low in self._VALUE_FLAGS:
                expect = "value"
            elif tok and not tok.startswith(('-', '$', '/')) and not self._PATH_LITERAL.match(tok):
                out.append(tok)
        return out

    def _disposable_statement(self, statement: str, variables: dict, cwd=None) -> bool:
        targets = [m.group(0) for m in self._PATH_LITERAL.finditer(statement)]
        for name, suffix in self._VAR_REF.findall(statement):
            name = name.lower()
            if name in variables:
                targets.append(variables[name] + (suffix if suffix.startswith(('\\', '/')) else ''))
            elif name not in self._NEUTRAL_VARS:
                return False                     # deletes something we cannot see: ask
        cwd_ok = cwd is not None and self._disposable(cwd)
        relative = self._relative_targets(statement)
        # Relative names resolve against the folder the command cd'd into: "cd D:\Agents\cato;
        # Remove-Item scratch\x" is inside scratch even though the cd folder itself is not.
        if relative and not (cwd is not None and
                             all(self._disposable(cwd.rstrip('\\/') + '\\' + r) for r in relative)):
            return False                         # relative target outside a known scratch dir: ask
        if not targets and not relative:
            return cwd_ok                        # e.g. "Get-ChildItem | Remove-Item" in the cwd
        return all(self._disposable(t) for t in targets)

    def _kills_by_name(self, command: str) -> bool:
        for statement in re.split(r'[;\n]', command or ""):
            if self._PROCESS_KILL.search(statement) and not self._PID_ONLY_KILL.match(statement):
                return True
        return False

    def _is_dangerous(self, command: str) -> bool:
        c = command or ""
        if self._CATASTROPHIC_CMD.search(c):
            return True
        if not self._DELETION_CMD.search(c):
            return False
        variables = {k.lower(): v for k, v in self._VAR_ASSIGN.findall(c)}
        cwd = None
        # Only mass deletions (recursive or wildcard) can prompt. A single-file delete, and any
        # file edit/write, run without asking -- that's routine work.
        for statement in re.split(r'[;\n]', c):
            cd = self._CD.match(statement)
            if cd:
                cwd = self._resolve_dir(cd.group(1), variables)
                continue
            if self._DELETION_CMD.search(statement) and self._MASS_DELETE.search(statement):
                if not self._disposable_statement(statement, variables, cwd):
                    return True
        return False

    def _kill_tree(self, pid: int) -> None:
        """taskkill /T takes the children and grandchildren too; a plain kill leaves them alive
        and still holding our pipes."""
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            pass

    def _with_deadline(self, fn, seconds: int, on_timeout=None, label: str = "tool"):
        """Run fn() on a worker thread and give up after `seconds`. The tool functions do their
        own CoInitialize, so COM is happy on the worker. The abandoned thread is a daemon: it
        dies with the process, or when on_timeout() kills whatever it was blocked on."""
        box: dict = {}

        def run():
            try:
                box["result"] = fn()
            except Exception as e:           # surface, don't swallow
                box["result"] = f"Error in {label}: {e}"

        t = threading.Thread(target=run, daemon=True, name=f"deadline-{label}")
        t.start()
        t.join(seconds)
        if t.is_alive():
            note = on_timeout() if on_timeout else ""
            print(f"[deadline] {label} exceeded {seconds}s; {note}")
            return (f"{label} did not respond within {seconds} seconds."
                    f"{' (' + note + ')' if note else ''} The worker may still finish; the outcome is UNKNOWN. "
                    f"Do not resend or repeat a change. Inspect saved email receipts, Outbox/Sent Items or the actual file first. "
                    f"Receipts: {self.AGENT_DIR / 'task_state' / 'email_receipts'}")
        return box.get("result", f"Error in {label}: no result")

    def tool_send_photo(self, chat_id: int, url: str = "", path: str = "", caption: str = "", marks=None, crop=None) -> str:
        try:
            if path:
                p = Path(path)
                if not p.is_file():
                    return f"No file at {path}"
                data, name = p.read_bytes(), p.name
            elif url:
                response = requests.get(url, timeout=15)
                response.raise_for_status()
                data, name = response.content, "image.jpg"
            else:
                return "Give path (a local image) or url."
            if marks or crop:
                data, name = mark_image(data, marks or [], crop), Path(name).stem + "_marked.png"
            image_data = io.BytesIO(data)
            image_data.name = name
            self.bot.send_photo(chat_id, image_data, caption=caption if caption else None)
            return "Photo sent." + (f" {len(marks)} mark(s) drawn." if marks else "")
        except requests.RequestException as e:
            return f"Failed to download image: {e}"
        except Exception as e:
            return f"Failed to send photo: {e}"

    def tool_edit_image(self, prompt: str, chat_id: int, quality: str = "medium") -> str:
        """Edit the last photo Alex sent, per his instruction, and send back the result."""
        if not self.OPENAI_API_KEY:
            return "Image editing needs OPENAI_API_KEY in .env."
        src = self._last_photo.get(chat_id)
        if not src:
            saved = self._images.list(chat_id, limit=1)
            if saved and Path(saved[0]["path"]).is_file():
                src = Path(saved[0]["path"]).read_bytes()
        if not src:
            return "No photo to edit — ask Alex to send the photo first, then say what to change."

        q = (quality or "medium").lower()
        if q not in ("low", "medium", "high"):
            q = "medium"
        try:
            from openai import OpenAI as OAClient
            oa = OAClient(api_key=self.OPENAI_API_KEY, timeout=300.0)
            img = io.BytesIO(src)
            img.name = "input.png"
            r = oa.images.edit(model=self.IMAGE_MODEL, image=img, prompt=prompt, quality=q, n=1)
            b64 = r.data[0].b64_json
            if not b64:
                return "Image API returned no image data."
            data = base64.b64decode(b64)
        except Exception as e:
            return f"Image edit failed: {e}"

        try:
            buf = io.BytesIO(data)
            buf.name = "edited.png"
            self.bot.send_photo(chat_id, buf, caption=prompt[:1000])
            return ("Edited image sent. Do not repeat the prompt back to Alex; he can see it.")
        except Exception as e:
            return f"Edited but failed to send: {e}"

    def tool_generate_image(self, prompt: str, chat_id: int, orientation: str = "square",
                            quality: str = "medium") -> str:
        """Generate an image with OpenAI's image model and send it to Alex on Telegram."""
        if not self.OPENAI_API_KEY:
            return "Image generation needs OPENAI_API_KEY in .env."

        size = {"landscape": "1536x1024", "portrait": "1024x1536"}.get(
            (orientation or "square").lower(), "1024x1024")
        q = (quality or "medium").lower()
        if q not in ("low", "medium", "high"):
            q = "medium"

        try:
            from openai import OpenAI as OAClient
            oa = OAClient(api_key=self.OPENAI_API_KEY, timeout=300.0)
            r = oa.images.generate(model=self.IMAGE_MODEL, prompt=prompt, size=size, quality=q, n=1)
            b64 = r.data[0].b64_json
            if not b64:
                return "Image API returned no image data."
            data = base64.b64decode(b64)
        except Exception as e:
            return f"Image generation failed: {e}"

        try:
            buf = io.BytesIO(data)
            buf.name = "image.png"
            self.bot.send_photo(chat_id, buf, caption=prompt[:1000])
            return (f"Image generated and sent ({size}, {q} quality). "
                    "Do not repeat the prompt back to Alex; he can see it.")
        except Exception as e:
            return f"Generated but failed to send: {e}"

    def tool_review_output(self, path: str = "", checks=None, claims=None, recalculate: bool = True,
                           render: bool = True, date_from: str = "", date_to: str = "") -> str:
        import uuid
        request = self.AGENT_DIR / "task_state" / "quality_requests" / (uuid.uuid4().hex + ".json")
        self.atomic_json(request, dict(path=path, checks=checks, claims=claims, recalculate=recalculate,
                                  render=render, date_from=date_from, date_to=date_to))
        try:
            proc = subprocess.Popen([sys.executable, str(self.AGENT_DIR / "scripts" / "review_output.py"), str(request)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                out, err = proc.communicate(timeout=180)
            except subprocess.TimeoutExpired:
                self._kill_tree(proc.pid)
                return Failed("Quality review timed out. The original file was not modified; quality is UNVERIFIED. Saved request: " + str(request))
            output = (out or b"").decode("utf-8", errors="replace").strip()
            if not output:
                return "Quality review failed; no completion claim is justified. " + (err or b"").decode("utf-8",errors="replace")[-1500:]
            return self._cap_result(output, limit=18000)
        except Exception as exc:
            return "Quality review unavailable: " + str(exc)

    def tool_send_document(self, path: str, chat_id: int, caption: str = "") -> str:
        """Send a local file (spreadsheet, PDF, doc, etc.) to Alex on Telegram."""
        try:
            p = Path(path)
            if not p.exists():
                return f"File not found: {path}"
            if p.stat().st_size > 45 * 1024 * 1024:
                return f"File too large to send via Telegram ({p.stat().st_size // (1024*1024)} MB; limit ~50 MB)."
            with open(p, "rb") as f:
                self.bot.send_document(chat_id, f, visible_file_name=p.name, caption=caption or None)
            return f"Sent {p.name}."
        except Exception as e:
            return f"Failed to send document: {e}"

    def _resolve_zsxq_group(self, group_url: str) -> tuple[str | None, str | None]:
        value = (group_url or "").strip()
        if not value or value in {"默认研报圈", "默认研报", "默认研报"}:
            return self.ZSXQ_DEFAULT_URL, None
        if not value.startswith(("https://", "http://")):
            return None, "That is a group name, not a URL. Omit group_url to use 默认研报圈."
        return value, None

    def tool_list_research_reports(self, date_from: str = "", date_to: str = "", group_url: str = "") -> str:
        today = datetime.now().strftime("%Y-%m-%d")
        date_from = date_from.strip() or today
        date_to   = date_to.strip() or today
        group_url, group_error = self._resolve_zsxq_group(group_url)
        if group_error:
            return group_error

        scan, err = self._run_zsxq("summaries", date_from, date_to, group_url, timeout=300)
        if err:
            return err
        if not isinstance(scan, dict) or not isinstance(scan.get("items"), list):
            return "Summary reader returned an unexpected format; summary coverage is unverified."
        posts = scan["items"]
        coverage = scan.get("coverage", {})
        lines = [f"{len(posts)} post summaries between {date_from} and {date_to}; no PDFs downloaded.",
                 "Coverage: " + json.dumps(coverage, ensure_ascii=False),
                 "If the date boundary was not reached or posts are undated/collapsed, describe this as a partial scan."]
        for i, post in enumerate(posts, 1):
            names = "; ".join(post.get("filenames", [])) or "Post without a new PDF filename"
            lines.append(f"\n{i}. [{post.get('date', '?')}] {names}\n"
                         f"Visible post summary{' (COLLAPSED / incomplete)' if post.get('summary_collapsed') else ''}:\n"
                         f"{post.get('summary', '')}")
        lines.append("\nDiscuss candidate themes and supporting summaries with Alex first. "
                     "Wait for his report selection before a PDF deep dive. If this scan is archived, "
                     "read its remaining sections before claiming to have reviewed every summary.")
        return self._cap_result("\n".join(lines), limit=40000)

    def tool_read_research_reports(self, date_from: str = "", date_to: str = "", group_url: str = "",
                                   filter: str = "", max_reports: int = 5,
                                   full: bool = False, preview_pages: int = 3) -> str:
        today = datetime.now().strftime("%Y-%m-%d")
        date_from = date_from.strip() or today
        date_to   = date_to.strip() or today
        group_url, group_error = self._resolve_zsxq_group(group_url)
        if group_error:
            return group_error
        if not filter.strip():
            return ("Summary-first workflow: no selected report filename filter was provided. "
                    "No PDFs will be downloaded. Scan summaries, discuss with Alex, then deep-read his choices.\n\n"
                    + self.tool_list_research_reports(date_from, date_to, group_url))
        max_reports = max_reports or 5

        # Budget ~2 min per report plus scroll/scan overhead, capped at 25 min.
        timeout = min(1500, 180 + max_reports * 120)
        reports, err = self._run_zsxq("read", date_from, date_to, group_url,
                                 name_filter=filter.strip(), max_reports=max_reports, timeout=timeout)
        if err:
            return err
        if not reports:
            hint = f" matching '{filter}'" if filter.strip() else ""
            return f"No PDFs retrieved{hint} between {date_from} and {date_to}. Scan coverage is unverified; this is not proof that no matching reports exist."

        # Persist full extraction to disk so nothing is lost to history trimming.
        try:
            self.ZSXQ_REPORTS_DIR.mkdir(exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_path = self.ZSXQ_REPORTS_DIR / f"reports_{stamp}.json"
            out_path.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
            saved_note = f"\n\n(Full text saved to {out_path} — read_file it to revisit any report.)"
        except Exception:
            saved_note = ""

        # Cost control: by default only the first few pages go into context; the full text is on
        # disk as a .txt per report (read it in slices with execute_command if more is needed).
        import re as _re
        preview_pages = max(1, int(preview_pages or 3))
        blocks = [f"Read {len(reports)} report(s) between {date_from} and {date_to}:\n"]
        for i, r in enumerate(reports, 1):
            text = r.get("text", "") or ""
            npages = r.get("total_pages", r.get("pages_read", 0))
            coverage = (f"\nExtraction coverage: {r.get('coverage', 'Legacy extraction; verify page coverage')}. "
                        f"Pages needing vision: {r.get('vision_pages', [])}. "
                        f"Pages containing visuals: {r.get('visual_pages', [])}. "
                        f"Original PDF: {r.get('pdf_path', 'not retained')}; "
                        f"Full extracted text: {r.get('text_path', 'see saved JSON')}. "
                        f"Render an exact page for view_image: {r.get('render_command', 'unavailable')}\n")
            note = ""
            if not full:
                cut = None
                m = _re.search(rf"\*\*Page {preview_pages + 1}\*\*", text)
                if m:
                    cut = m.start()
                if cut is None or cut > 9000:
                    cut = min(len(text), 9000)
                if cut < len(text):
                    try:
                        txt_path = self.ZSXQ_REPORTS_DIR / f"reports_{stamp}_{i}.txt"
                        txt_path.write_text(text, encoding="utf-8")
                        where = str(txt_path)
                    except Exception:
                        where = str(out_path)
                    note = (f"\n\n[PREVIEW: first {cut} of {len(text)} chars shown (about the first "
                            f"{preview_pages} of {npages} pages). Full text: {where} . Read further slices "
                            f"only if needed, or re-call with full=true if Alex asks for the whole report.]")
                    text = text[:cut]
            blocks.append(
                f"=== {r.get('filename', '?')} ({r.get('date', '?')}, {npages} pages) ===\n"
                f"{coverage}{text}{note}"
            )
        return "\n\n".join(blocks) + saved_note

    def tool_web_fetch(self, url: str, query: str = "") -> str:
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            from tabularium.tools.web_content import visible_text, page_result
            text = visible_text(resp.content) if "html" in content_type else resp.text
            return page_result(text, resp.url, self.RUNTIME_DIR / "context_archive" / "results", query)
        except Exception as e:
            return Failed(f"web_fetch failed: {type(e).__name__}: {e}")

    def _tavily_quota_state(self):
        # Shared by foreground/background workers and retained through restart.
        path = self.AGENT_DIR / "task_state" / "search_quota.json"
        return path

    def _tavily_usage_exhausted(self):
        response = requests.get("https://api.tavily.com/usage",
                                headers={"Authorization": "Bearer " + self.TAVILY_API_KEY}, timeout=15)
        response.raise_for_status()
        data = response.json()
        key, account = data.get("key", {}), data.get("account", {})
        limit = key.get("limit")
        key_full = limit is not None and key.get("usage", 0) >= limit
        limit = account.get("plan_limit")
        plan_full = limit is not None and account.get("plan_usage", 0) >= limit
        # Never enable paid Tavily overages ourselves; successful searches confirm recovery.
        return key_full or plan_full

    def tool_web_search_tavily(self, query: str, chat_id: int = 0) -> str:
        path = self._tavily_quota_state()
        try:
            with memory_lock(path):
                try:
                    state = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    state = {}
                blocked = state.get("exhausted", False)
                if blocked and time.time() >= state.get("check_after", 0):
                    try:
                        blocked = self._tavily_usage_exhausted()
                    except requests.RequestException:
                        pass  # Keep native search available if quota lookup is unavailable.
                    state.update(exhausted=blocked, check_after=time.time() + 24 * 60 * 60)
                    self.atomic_json(path, state)
            if blocked:
                return self._search_after_tavily(query, chat_id)
            try:
                response = self.tavily_client.search(query, max_results=5)
            except Exception as exc:
                from tavily.errors import UsageLimitExceededError, ForbiddenError
                if not isinstance(exc, (UsageLimitExceededError, ForbiddenError)):
                    raise
                # Tavily also uses these exceptions for rate limits/forbidden requests.
                # Confirm credit exhaustion instead of treating every error as quota.
                if not self._tavily_usage_exhausted():
                    raise
                with memory_lock(path):
                    self.atomic_json(path, {"exhausted": True, "check_after": time.time() + 24 * 60 * 60})
                return self._search_after_tavily(query, chat_id)
            results = response.get("results", [])
            if not results:
                return "Tavily: no results found."
            return "Search provider: Tavily\n" + "\n".join(
                f"Title: {r.get('title', '')}\nURL: {r.get('url', '')}\nContent: {r.get('content', '')}\n"
                for r in results)
        except Exception as exc:
            print(f"[search] Tavily failed ({type(exc).__name__}); trying the next engine.")
            try:
                return self._search_after_tavily(query, chat_id)
            except Exception as exc2:
                return f"Search unavailable ({type(exc2).__name__}). Do not treat this as no results."

    def _anysearch_state_path(self):
        return self.AGENT_DIR / "task_state" / "anysearch_state.json"

    def tool_web_search_anysearch(self, query: str):
        """AnySearch (api.anysearch.com), second in the search order (Alex, 2026-10-06):
        Tavily -> AnySearch -> OpenAI native web_search. Returns formatted results, or None when
        unavailable or empty so the caller moves on. Rate/quota/auth refusals pause it briefly
        (task_state/anysearch_state.json) instead of being retried on every search. Works
        anonymously at lower limits; ANYSEARCH_API_KEY in .env raises them."""
        path = self._anysearch_state_path()
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if state.get("retry_at", 0) > time.time():
            return None
        headers = {"Content-Type": "application/json", "X-Anysearch-Client": "agents/1.0"}
        if self.ANYSEARCH_API_KEY:
            headers["Authorization"] = "Bearer " + self.ANYSEARCH_API_KEY
        try:
            resp = requests.post(self.ANYSEARCH_URL, headers=headers, timeout=30,
                                 json={"query": query, "max_results": 8})
            body = resp.json() if resp.content else {}
        except (requests.RequestException, ValueError) as exc:
            print(f"[search] AnySearch unavailable: {type(exc).__name__}")
            return None
        if not isinstance(body, dict):
            body = {}
        if resp.status_code >= 400 or body.get("code", 0) != 0:
            msg = str(body.get("message") or f"HTTP {resp.status_code}")
            low = msg.lower()
            pause = (3600 if resp.status_code in (401, 403) else
                     1800 if resp.status_code == 429 or any(w in low for w in ("quota", "rate", "limit")) else 0)
            if pause:
                self.atomic_json(path, {"retry_at": time.time() + pause, "reason": msg[:200]})
            print(f"[search] AnySearch refused (HTTP {resp.status_code}): {msg[:120]}")
            return None
        results = (body.get("data") or {}).get("results") or []
        if not results:
            return None
        return "Search provider: AnySearch\n" + "\n".join(
            f"Title: {r.get('title', '')}\nURL: {r.get('url', '')}\n"
            f"Content: {r.get('content') or r.get('snippet') or ''}\n" for r in results)

    def _search_after_tavily(self, query: str, chat_id: int) -> str:
        # AnySearch was already tried before Tavily; go on to the model routes.
        return self._native_search(query, chat_id)

    def _native_search(self, query: str, chat_id: int) -> str:
        """Model-native web search in this agent's own route order -- ChatGPT subscription,
        Claude subscription, OpenAI API, Anthropic API, as saved in routing.json. Shared logic in
        D:\\Agents\\search_routing.py."""
        from tabularium.tools import search_routing
        return search_routing.model_search(self, query, chat_id)

    def tool_web_search(self, query: str, chat_id: int = 0) -> str:
        """Engines in the order set on the dashboard (search_order.json, shared by all four agents).
        Default AnySearch -> Tavily -> model routes (Alex, 2026-10-06/07)."""
        from tabularium.tools import search_routing as _search
        for engine in _search.read_order():
            if engine == "anysearch":
                result = self.tool_web_search_anysearch(query)
                if result is not None:
                    return result
            elif engine == "tavily" and self.TAVILY_API_KEY:
                return self.tool_web_search_tavily(query, chat_id)
            elif engine == "model":
                return self._native_search(query, chat_id)
        return "Search unavailable: no engine in the configured order could run (AnySearch returned nothing, Tavily has no key, model routes not in the order)."

    def _browser_shutdown(self) -> None:
        """Clean up Playwright on process exit."""
        pass  # state belongs to the runtime
        try:
            if self._page_instance and not self._page_instance.is_closed():
                self._page_instance.close()
            if self._browser_instance and not self._browser_attached:
                self._browser_instance.close()
            if self._playwright_instance:
                self._playwright_instance.stop()
        except Exception:
            pass
        self._playwright_instance = None
        self._browser_instance    = None
        self._page_instance       = None

    def tool_browser_navigate(self, url: str, wait_for: str = "load", authenticated: bool = False) -> str:
        try:
            with self._browser_lock:
                page = self._get_page(authenticated=authenticated)
                wait_for = wait_for if wait_for in ("load", "networkidle", "domcontentloaded") else "load"
                page.goto(url, wait_until=wait_for, timeout=30000)
                title   = page.title()
                current = page.url
                content = page.evaluate("() => document.body ? document.body.innerText : ''")
                content = content.strip()
                if len(content) > 16000:
                    content = content[:16000] + "\n...[truncated at 16000 chars]"
                return f"Title: {title}\nURL: {current}\n\n{content}"
        except Exception as e:
            return f"Browser navigate error: {e}"

    def tool_browser_screenshot(self, chat_id: int) -> str:
        try:
            with self._browser_lock:
                page = self._get_page()
                data  = page.screenshot(type="png")
            img = io.BytesIO(data)
            img.name = "screenshot.png"
            self.bot.send_photo(chat_id, img)
            return "Screenshot sent."
        except Exception as e:
            return f"Screenshot error: {e}"

    def tool_browser_click(self, selector: str) -> str:
        try:
            with self._browser_lock:
                page = self._get_page()
                page.click(selector, timeout=10000)
                try:
                    page.wait_for_load_state("load", timeout=8000)
                except Exception:
                    pass
                title   = page.title()
                current = page.url
                content = page.evaluate("() => document.body ? document.body.innerText : ''")
                content = content.strip()[:4000]
                return f"Clicked '{selector}'.\nTitle: {title}\nURL: {current}\n\n{content}"
        except Exception as e:
            return f"Click error: {e}"

    def tool_browser_close(self) -> str:
        """Explicitly close the browser and free resources."""
        pass  # state belongs to the runtime
        # Other workers can be attached to this headless context.
        with self._browser_lock:
            if self._page_instance and not self._page_instance.is_closed():
                self._page_instance.close()
            self._page_instance = None
        return "This task's browser page closed. Shared authenticated browser remains available."

    def tool_browser_type(self, selector: str, text: str) -> str:
        try:
            with self._browser_lock:
                page = self._get_page()
                page.fill(selector, text, timeout=10000)
                return f"Typed into '{selector}'."
        except Exception as e:
            return f"Type error: {e}"

    def _block_to_dict(self, block) -> dict:
        """Convert an Anthropic SDK content block to a plain JSON-serialisable dict."""
        if isinstance(block, dict):
            return block
        if hasattr(block, 'model_dump'):
            return block.model_dump()
        result = {}
        for attr in ('type', 'text', 'id', 'name', 'input', 'content', 'tool_use_id'):
            if hasattr(block, attr):
                val = getattr(block, attr)
                result[attr] = dict(val) if hasattr(val, 'items') else val
        return result

    def _serialise_messages(self, messages: list) -> list:
        out = []
        for msg in messages:
            content = msg['content']
            if isinstance(content, str):
                out.append({'role': msg['role'], 'content': content})
            elif isinstance(content, list):
                out.append({'role': msg['role'], 'content': [self._block_to_dict(b) for b in content]})
        return out

    def _conversation_path(self, chat_id: int) -> Path:
        return self.RUNTIME_DIR / f"conversation_{chat_id}.json"

    def save_conversation(self, chat_id: int) -> None:
        # Never rebind or truncate the live list while a tool loop holds a reference to it.
        # Atomic replacement keeps the previous complete file if writing is interrupted.
        serialised = self._serialise_messages(self.conversations.get(chat_id, []))
        self.atomic_json(self._conversation_path(chat_id), serialised)
        self._recall.record(chat_id, serialised, self._conversation_path(chat_id))

    def load_conversation(self, chat_id: int) -> list:
        path = self._conversation_path(chat_id)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(data, list) and data:
                    print(f"Conversation restored for chat {chat_id} ({len(data)} messages).")
                    return self.recover_tool_history(data, self.RUNTIME_DIR)
            except Exception as e:
                print(f"Failed to load conversation: {e}")
        return self.load_memory_context()

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
        if len(recent) == self._REPEAT_LIMIT and all(k == key for k, _ in recent) and len({d for _, d in recent}) == 1:
            return self._note_outcome(name, Refused(
                f"NOT executed: this exact {name} call has already run {self._REPEAT_LIMIT} times in a row with the identical "
                "result. Repeating it cannot change the outcome. Read the previous result, change the inputs or the tool, "
                "or report the blocker and stop."), chat_id)
        try:
            result = self._execute_tool_unguarded(name, inputs, chat_id)
            taint.mark(self, chat_id, name)               # a reader of external content taints the turn (tabularium.core.taint)
            result = bound_result(result, self.AGENT_DIR / "context_archive" / "results")
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

    def trim_history(self, messages: list, chat_id: int) -> list:
        """Retain complete recent user exchanges; completed tool traces do not evict the conversation."""
        from tabularium.core.context_window import exchange_starts, completed_exchanges
        starts = exchange_starts(messages)
        anchor = self._window_anchor.get(chat_id)
        position = next((n for n, i in enumerate(starts) if id(messages[i]) == anchor), None)
        if position is not None and len(starts) - position <= self.WINDOW_HIGH:
            start = starts[position]
        elif len(starts) <= self.WINDOW_HIGH:
            start = 2
        else:
            start = starts[-self.WINDOW_LOW]
            self._window_anchor[chat_id] = id(messages[start])
        window = completed_exchanges(messages[:2] + messages[start:],
                                     self.AGENT_DIR / 'context_archive' / str(chat_id))
        return self._attach_current_context(window, chat_id)

    def _attach_current_context(self, window: list, chat_id: int) -> list:
        """What is true now goes on the wire copy, next to the newest message: this turn's notes, and the core memory
        read fresh from memory.md. Nothing here is saved, so a correction is in force from the next request on and no
        older copy can contradict it. The history above stays unchanged, so it stays cacheable."""
        self._context_sizes = {}
        if getattr(self, "_trial", None):
            return window
        window = self._pin_contract(window[:2]) + window[2:]
        window = self._attach_turn_notes(window, chat_id)
        store = getattr(self, "_background", None)
        live = store.live_context(chat_id, int(self.BACKGROUND_TASK_ID) if self.BACKGROUND_TASK_ID else None) if store is not None else ""
        if self.BACKGROUND_TASK_ID and store is not None:
            from tabularium.records import task_cost
            budget_note = task_cost.notice(store.directory(int(self.BACKGROUND_TASK_ID)))
            if budget_note:
                live += '\n' + budget_note
        if not self.BACKGROUND_TASK_ID:
            recalled = self._recall_affairs(window, chat_id)
            if recalled:
                live = (live + "\n\n" + recalled) if live else recalled
        try:
            from tabularium.learning import corrections
            burst = corrections.burst_note(self, chat_id) if store is not None else ""
        except (OSError, ValueError) as exc:
            burst = ""; print(f"[correction] burst note: {type(exc).__name__}: {exc}")
        if not self.BACKGROUND_TASK_ID and hasattr(self._inbox, 'turn_sources'):
            incoming = self._inbox.turn_sources(chat_id)
            if incoming:
                live += '\n[Incoming messages: ' + json.dumps([
                    {'id': m['message_id'], 'text': m['text']} for m in incoming], ensure_ascii=False) + ']'
        if burst:
            live = (live + "\n\n" + burst) if live else burst
        if live:
            for i in range(len(window) - 1, -1, -1):
                if window[i].get("role") == "user":
                    content = window[i].get("content")
                    content = content + "\n\n" + live if isinstance(content, str) else list(content or []) + [{"type": "text", "text": live}]
                    window = window[:i] + [{**window[i], "content": content}] + window[i+1:]
                    break
        core = self._load_core_memory()
        self._context_sizes.update(core_chars=len(core), live_state_chars=len(live),
                                   turn_notes_chars=len(self._turn_notes.get(chat_id, ("", ""))[1]))
        if not core:
            return window
        block = f"{self._CORE_MEMORY_NOTE}\n\n{core}\n\n[End of core memory. The message follows.]\n\n"
        for i in range(len(window) - 1, -1, -1):
            message = window[i]
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                return window[:i] + [{**message, "content": block + content}] + window[i + 1:]
            if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") != "tool_result" for b in content):
                return window[:i] + [{**message, "content": [{"type": "text", "text": block.rstrip()}] + content}] + window[i + 1:]
        return window

    def _recall_affairs(self, window: list, chat_id: int) -> str:
        """A message about one of Alex's standing matters (core memory, "Alex's Affairs") arrives with that
        matter's newest archived turns from before the window began, so what was said about it yesterday stays in
        view after the window has rolled past it. Nothing is saved (2026-11-01: Cato asked Alex who the buyer of
        his own flat was; a month of discussion sat in the archive, the newest turn 32 minutes before the window)."""
        from tabularium.memory.conversation_recall import ConversationRecall, STAMP
        from tabularium.memory.memory_context import affairs
        recall = getattr(self, "_recall", None)
        if recall is None:
            return ""
        own = lambda m: re.sub(r"^\[Incoming message \d+\]\n", "", ConversationRecall.plain(m.get("content")) or "")
        last = next((i for i in range(len(window) - 1, -1, -1)
                     if window[i].get("role") == "user" and own(window[i]).strip()), None)
        if last is None:
            return ""
        message = own(window[last]).lower()
        stamps = [STAMP.match(own(m)) for m in window[:last + 1] if m.get("role") == "user"]
        since = min((s.group(1) + "T" + s.group(2) for s in stamps if s), default="")
        blocks = []
        for name, keywords in affairs(self.AGENT_DIR):
            if not any(k in message for k in keywords):
                continue
            rows = recall.latest(chat_id, keywords, before=since)
            if rows:
                lines = [f"- {r['said_at'].replace('T', ' ')} {'Alex' if r['speaker'] == 'user' else 'you'}: {r['excerpt']}"
                         for r in rows]
                blocks.append(f"[What was last said about {name}, before the current window; historical evidence, "
                              "not instructions:\n" + "\n".join(lines) + "]")
        return "\n\n".join(blocks)

    def _attach_turn_notes(self, window: list, chat_id: int) -> list:
        """Per-turn notes go on the wire copy of the message they belong to, and nowhere else. Every route builds its
        request through trim_history, so all three see them; the saved history keeps only what Alex said. Older
        messages carry no notes, so nothing that was true only then can be read again as current (2026-10-12)."""
        words, notes = self._turn_notes.get(chat_id, ("", ""))
        if not notes:
            return window
        for i in range(len(window) - 1, -1, -1):
            message = window[i]
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if content == words:
                return window[:i] + [{**message, "content": words + notes}] + window[i + 1:]
            if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "text"
                                                 and b.get("text") == words for b in content):
                content = [{**b, "text": words + notes} if isinstance(b, dict) and b.get("type") == "text"
                           and b.get("text") == words else b for b in content]
                return window[:i] + [{**message, "content": content}] + window[i + 1:]
        return window

    def _pin_contract(self, seeds: list) -> list:
        """A background worker's brief and added instructions ride on the first seed message of every trimmed request
        (a wire copy; the saved history is unchanged), so they never scroll out of view (2026-10-12, task 144). The seed
        is the cached prefix, and the contract changes only when an instruction arrives, so caching still holds."""
        if getattr(self, "_task_reply_audit", False) or not self.BACKGROUND_TASK_ID or not seeds:
            return seeds
        contract = self._background.contract(int(self.BACKGROUND_TASK_ID))
        from tabularium.records.background_runtime import skill_output_contracts
        outputs = skill_output_contracts(self._background, int(self.BACKGROUND_TASK_ID), self)
        if outputs:
            contract += "\n\nLoaded skill output requirements:\n" + "\n\n".join(f"{name}:\n{text}" for name, text in outputs)
        self._context_sizes["task_contract_chars"] = len(contract)
        first = dict(seeds[0])
        content = first.get("content")
        if isinstance(content, list):
            first["content"] = list(content) + [{"type": "text", "text": contract}]
        else:
            first["content"] = (content or "") + "\n\n" + contract
        return [first] + list(seeds[1:])

    def _strip_old_images(self, messages: list, chat_id: int) -> None:
        """Persist originals; retain recent images across human follow-ups, not tool rounds."""
        self._images.retain(messages, chat_id)

    def _apply_cache_to_history(self, messages: list) -> list:
        """Add only the memory and current history breakpoints to a fresh wire copy."""
        result = clear_cache_markers(messages)
        if len(result) < 2:
            return result
        for idx in dict.fromkeys((0, len(result) - 2)):
            content = result[idx]["content"]
            if isinstance(content, str) and content:
                result[idx]["content"] = [{"type": "text", "text": content,
                                           "cache_control": dict(self.CACHE_CONTROL)}]
            elif isinstance(content, list) and content:
                content[-1]["cache_control"] = dict(self.CACHE_CONTROL)
        return result

    def _block_attr(self, block, name):
        return block.get(name) if isinstance(block, dict) else getattr(block, name, None)

    def _clean_block(self, block):
        """Reduce an SDK content block to the fields the API accepts on input."""
        btype = self._block_attr(block, "type")
        if btype not in self._BLOCK_FIELDS:
            return block if isinstance(block, dict) else self._block_to_dict(block)
        out = {"type": btype}
        for f in self._BLOCK_FIELDS[btype]:
            v = self._block_attr(block, f)
            if v is not None:
                out[f] = v
        return out

    def _clean_content(self, content):
        """Sanitise an assistant message's content before it enters history."""
        if isinstance(content, list):
            return [self._clean_block(b) for b in content]
        return content

    def _oa_tools_for_responses(self) -> list:
        """chat.completions tool shape -> Responses tool shape (flattened)."""
        out = []
        for t in self.get_tools("openai"):
            f = t.get("function", t)
            out.append({"type": "function", "name": f["name"],
                        "description": f.get("description", ""),
                        "parameters": f.get("parameters", {"type": "object", "properties": {}})})
        return out

    def _image_block_to_data_uri(self, block: dict) -> str | None:
        src = block.get("source") or {}
        if src.get("type") == "base64" and src.get("data"):
            return f"data:{src.get('media_type', 'image/png')};base64,{src['data']}"
        return None

    def _history_to_responses_items(self, msgs: list) -> list:
        """Anthropic-format history -> Responses API input items."""
        items = []
        for m in msgs:
            role, content = m["role"], m["content"]
            if isinstance(content, str):
                if content:
                    items.append({"role": role, "content": content})
                continue
            if role == "assistant":
                text = "".join(self._block_attr(b, "text") or "" for b in content if self._block_attr(b, "type") == "text")
                if text.strip():
                    items.append({"role": "assistant", "content": text})
                for b in content:
                    if self._block_attr(b, "type") == "tool_use":
                        items.append({"type": "function_call", "call_id": self._block_attr(b, "id"),
                                      "name": self._block_attr(b, "name"),
                                      "arguments": json.dumps(self._block_attr(b, "input") or {}, ensure_ascii=False)})
                continue
            # user message: text / image parts become one user item; tool_results become
            # function_call_output items (an image inside a tool_result rides along as a user item)
            parts, trailing_images = [], []
            for b in content:
                t = self._block_attr(b, "type")
                if t == "text":
                    parts.append({"type": "input_text", "text": self._block_attr(b, "text") or ""})
                elif t == "image":
                    uri = self._image_block_to_data_uri(b)
                    parts.append({"type": "input_image", "image_url": uri} if uri
                                 else {"type": "input_text", "text": "[image unavailable]"})
                elif t == "tool_result":
                    inner = self._block_attr(b, "content")
                    if isinstance(inner, list):
                        texts = []
                        for x in inner:
                            if self._block_attr(x, "type") == "text":
                                texts.append(self._block_attr(x, "text") or "")
                            elif self._block_attr(x, "type") == "image":
                                uri = self._image_block_to_data_uri(x)
                                if uri:
                                    trailing_images.append({"type": "input_image", "image_url": uri})
                        inner = "\n".join(texts) or "(see image)"
                    items.append({"type": "function_call_output", "call_id": self._block_attr(b, "tool_use_id"),
                                  "output": str(inner)})
            if parts:
                items.append({"role": "user", "content": parts})
            if trailing_images:
                items.append({"role": "user", "content": [{"type": "input_text", "text": "[image returned by the tool above]"}] + trailing_images})
        return items

    def _is_context_window_error(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return any(term in text for term in (
            "context window", "context_length_exceeded", "prompt is too long", "maximum context length")) or (
            "input" in text and "context" in text and ("exceed" in text or "too long" in text))

    def _prepare_request_context(self, messages: list, chat_id: int) -> list:
        budget = self.WORKER_REQUEST_BYTES if self.BACKGROUND_TASK_ID else REQUEST_BYTES
        result = prepare_context(messages, self.AGENT_DIR / "context_archive" / str(chat_id), budget_bytes=budget)
        # Counts only: never persist memory or conversation text in diagnostics. This is the
        # host request/session-start size, not provider tokens or the SDK's internal turns.
        sizes = dict(self._context_sizes, system_chars=len(self.SYSTEM_PROMPT),
                     tools_chars=len(json.dumps(self.get_tools("anthropic"), ensure_ascii=False)),
                     messages_chars=len(json.dumps(result, ensure_ascii=False)))
        atomic_text(Path(self.RUNTIME_DIR) / "task_state" / "context_sizes.json", json.dumps(sizes))
        return result

    def _compact_context_retry(self, messages: list, chat_id: int) -> list:
        compact = prepare_context(messages, self.AGENT_DIR / "context_archive" / str(chat_id), retry=True)
        if wire_size(compact) >= wire_size(messages):
            raise RuntimeError(
                "The request still exceeds the model context limit, and reducing it further would "
                "remove task instructions, findings or recent results. History is preserved; "
                "I stopped rather than silently forget the task.")
        return compact

    def run_turn_openai(self, messages: list, chat_id: int) -> str:
        return turn_loop.run(self, messages, chat_id, turn_loop.OpenAIResponses(self, chat_id))

    def run_turn(self, messages: list, chat_id: int) -> str:
        # The loop guards count within one turn: a failure in Alex's last request must not carry a
        # streak into his next one, and a resumed worker starts clean.
        self._completion_feedback.pop(chat_id, None)
        self._repeat_calls.pop(chat_id, None)
        self._unproductive_streak.pop(chat_id, None)
        from tabularium.core import taint
        taint.reset(self, chat_id)
        from tabularium.core.session_lifecycle import continue_turn
        result = continue_turn(self, messages, chat_id, lambda: self._routing.run(self, messages, chat_id))
        if self.BACKGROUND_TASK_ID:
            return result
        corrected = getattr(result, "correction", ())
        from tabularium.records.task_decisions import verify_reply
        result = verify_reply(self, messages, chat_id, result)
        if corrected:
            from tabularium.learning import corrections
            try:
                corrections.start(self, chat_id, corrected)
            except (ValueError, OSError) as exc:     # the reply still goes out; the correction is logged as missed
                print(f"[correction] not recorded: {type(exc).__name__}: {exc}")
        return result

    def _parse_frontmatter(self, text: str):
        """Return (meta_dict, body). Minimal YAML: flat `key: value`, values may wrap."""
        if not text.startswith("---"):
            return {}, text
        end = text.find("\n---", 3)
        if end == -1:
            return {}, text
        raw, body = text[3:end], text[end + 4:]
        meta, key = {}, None
        for line in raw.splitlines():
            if not line.strip():
                continue
            m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
            if m:
                key = m.group(1).strip()
                val = m.group(2).strip()
                # YAML block scalars (`key: |` / `key: >`) carry their text on following lines.
                meta[key] = "" if val in ("|", ">", "|-", ">-") else val
            elif key and line.startswith((" ", "\t")):
                meta[key] = (meta[key] + " " + line.strip()).strip()   # wrapped / block value
        return meta, body.lstrip("\n")

    def discover_skills(self) -> dict:
        """Scan skills/ and return {name: {description, path, body}}."""
        found = {}
        if not self.SKILLS_DIR.exists():
            return found
        for d in sorted(self.SKILLS_DIR.iterdir()):
            f = d / "SKILL.md"
            if not (d.is_dir() and f.exists()):
                continue
            try:
                from tabularium.learning.trials import overridden
                source = Path(overridden(f) or f)                   # a trial arm reads the proposed edit (tabularium.learning.trials)
                meta, body = self._parse_frontmatter(source.read_text(encoding="utf-8-sig"))   # editors add a BOM; it hid two descriptions (2026-10-10)
            except Exception as e:
                print(f"[skills] cannot read {f}: {e}")
                continue
            name = meta.get("name") or d.name
            found[name] = {"description": meta.get("description", ""), "path": f, "body": body}
        return found

    def skills_prompt_block(self) -> str:
        """Layer 1 — what goes into the system prompt: the full body of every ALWAYS_SKILLS skill (a method the agent must
        follow in every conversation is standing instruction, not something to remember to load; Tacitus, 2026-10-09),
        then the catalogue of the rest."""
        sk = self.discover_skills()
        if not sk:
            return ""
        lines = []
        standing = [n for n in self.ALWAYS_SKILLS if n in sk]
        for n in standing:
            lines.append(f"\n\n## Standing method: {n} (always loaded, follow it in every conversation)\n"
                         f"Its references/ folder ({sk[n]['path'].parent / 'references'}) is read with read_file when the method points to it.\n\n"
                         + sk[n]["body"])
        sk = {n: s for n, s in sk.items() if n not in standing}
        if not sk:
            return "\n".join(lines)
        lines += ["\n\n## Available Skills\n",
                  "Installed methods: " + ", ".join(sk) + ".",
                  'Use skill(name="") to discover their descriptions when choosing a method; '
                  'use skill(name="exact-name") to load it before acting. '
                  'Do not guess a method from its name or treat an unloaded method as instructions.']
        return "\n".join(lines)

    def tool_skill(self, name: str, chat_id: int = None) -> str:
        """Layer 2 — load one skill's full instructions, with the agent's own earlier work with it (2026-10-21)."""
        sk = self.discover_skills()
        if not name:
            return "\n".join(f"- {n}: {s['description']}" for n, s in sk.items()) or "No skills installed."
        if name not in sk:
            avail = ", ".join(sk) or "(none installed)"
            return f"No skill named '{name}'. Installed skills: {avail}"
        s = sk[name]
        text = (f"# Skill: {name}\n(loaded from {s['path']})\n"
                f"Supporting files in that folder — references/, scripts/ — are read with read_file "
                f"or run with execute_command, only if the instructions below point to them.\n\n{s['body']}")
        import sqlite3, zipfile
        from tabularium.records import skill_record
        scope, ref = (("task", int(self.BACKGROUND_TASK_ID)) if self.BACKGROUND_TASK_ID
                      else ("turn", getattr(getattr(self._inbox, "local", None), "job", None)))
        try:
            history = skill_record.record(self.AGENT_DIR, name, chat_id, scope, ref,
                                          getattr(self, "_background", None), self._inbox)
            skill_record.note(self.AGENT_DIR, name, scope, ref, chat_id)
        except (OSError, sqlite3.Error, ValueError, zipfile.BadZipFile) as exc:     # the skill still loads
            print(f"[skill record] {name}: {type(exc).__name__}: {exc}")
            history = ""
        return text + history

    def load_memory_context(self) -> list:
        """The fixed opening of every conversation. It holds no copy of memory: a copy here went stale while the
        corrections appended after it scrolled out of view (2026-10-12). The current core memory is attached to the
        latest message of every request instead (_attach_current_context)."""
        session_start = datetime.now().strftime("%Y-%m-%d %H:%M")
        text = (f"[Session started: {session_start}]\n[Your core memory is attached to the latest message of every "
                "request, read fresh from memory.md each time, so it is always the current version.]")
        return [
            {"role": "user", "content": [
                {"type": "text", "text": text, "cache_control": dict(self.CACHE_CONTROL)},
            ]},
            {"role": "assistant", "content": "Core memory loaded. Ready."},
        ]

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

    def _refresh_core_memory(self, messages, chat_id):
        """Make sure the conversation opens with the fixed seed (rewritten once per process start). Memory itself is
        never stored in history; _attach_current_context puts the current core on every request."""
        seeded = len(messages) >= 2 and messages[1].get("content") in ("Core memory loaded. Ready.", "Memory loaded. Ready.")
        if not seeded:
            messages[:0] = self.load_memory_context()
        elif chat_id not in self._core_versions:
            messages[:2] = self.load_memory_context()
        self._core_versions[chat_id] = True

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
            if self._background.get(ident)["status"] in ("cancelled", "cancelling"):
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
            for ident, message in self._inbox.follow_ups(chat_id, job):
                content = self.compose_turn(message, nudge=False)
                if not isinstance(content, str):          # a reply to a photo stays its own turn
                    break
                entry = {"role": "user", "content": content}
                messages.append(entry)
                api_messages.append(entry)
                fresh.append(entry)
                taken.append(ident)
                local = getattr(self._inbox, "local", None)
                if local is not None:
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
            self.send_message(chat_id, f'Task stopped: {e}. The request and tool records are preserved.')
            return None
        finally:
            self._abort_requested.pop(chat_id, None)
            self.save_conversation(chat_id)

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

    def _nightly_save_worker(self) -> None:
        """Background thread: watches for save_requested.flag and runs the memory save.

        Uses a blocking lock.acquire() so it waits for any in-progress turn to
        finish before starting — no race conditions, no timing games.
        """
        print("Nightly save worker started.")
        while True:
            try:
                time.sleep(10)
                if not self.SAVE_REQUEST_FILE.exists():
                    continue

                print("Nightly save requested — waiting for chat lock...")

                try:
                    chat_id = int(self.CHAT_ID_FILE.read_text(encoding="utf-8").strip())
                except Exception:
                    # No active chat — confirm immediately so the restart can proceed
                    self.SAVE_REQUEST_FILE.unlink(missing_ok=True)
                    self.SAVE_CONFIRM_FILE.write_text("done", encoding="utf-8")
                    print("Nightly save: no active chat — confirmed without saving.")
                    continue

                print(f"Nightly save: acquiring lock for chat {chat_id}...")
                self.ensure_initialised(chat_id)
                lock = self.get_chat_lock(chat_id)
                lock.acquire()  # blocking — waits for any in-progress turn to finish
                print("Nightly save: lock acquired, running save turn...")
                try:
                    self.SAVE_REQUEST_FILE.unlink(missing_ok=True)
                    messages = self.conversations[chat_id]
                    messages.append({"role": "user", "content": f"{self._ts()} {self.NIGHTLY_SAVE_PROMPT}"})
                    self.bot.send_chat_action(chat_id, "typing")
                    reply = self._run_turn_with_guard(messages, chat_id)
                    if reply:
                        self.save_conversation(chat_id)
                        self.send_message(chat_id, reply)
                        if getattr(reply, "complete", False):      # the turn ran to its own end, not to a limit
                            self.SAVE_CONFIRM_FILE.write_text("done", encoding="utf-8")
                            self._conversation_path(chat_id).unlink(missing_ok=True)
                            self.send_message(chat_id, "Memory saved. Restarting shortly.")
                            print("Nightly save: complete.")
                        else:
                            print(f"Nightly save: the save turn did not complete: {reply[:100]}")
                    else:
                        print("Nightly save: _run_turn_with_guard returned None.")
                finally:
                    lock.release()

            except Exception as e:
                print(f"Nightly save worker error: {e}")

    def _recover_recent_photos(self):
        try:
            from tabularium.channels.owner_telegram import OWNER_CHAT_ID
            recovered = self._images.recover_telegram(self.bot, OWNER_CHAT_ID)
            print(f"[image-memory] Recovered {recovered} earlier Telegram photo(s).")
        except Exception as exc:
            print(f"[image-memory] Recovery unavailable: {type(exc).__name__}")

    def _run_zsxq(self, mode: str, date_from: str, date_to: str, group_url: str,
                  name_filter: str = "", max_reports: int = 0, timeout: int = 600):
        """Run zsxq_reader.py and return (parsed_json, error_string). One of them is None."""
        args = [sys.executable, str(self.ZSXQ_SCRIPT), "--mode", mode,
                "--group-url", group_url, "--date-from", date_from, "--date-to", date_to]
        if name_filter:
            args += ["--filter", name_filter]
        if max_reports:
            args += ["--max-reports", str(max_reports)]
        import hashlib
        scope = dict(mode=mode, group_url=group_url, date_from=date_from, date_to=date_to,
                     filter=name_filter, max_reports=max_reports)
        key = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()[:24]
        checkpoint = self.AGENT_DIR / "reports" / "research_checkpoints" / (key + ".json")
        args += ["--checkpoint", str(checkpoint)]
        # The reader attaches over CDP to the bot's own browser (port 9222), so make sure it is up.
        try:
            with self._browser_lock:
                self._get_page(authenticated=True)
        except Exception as e:
            return None, f"Could not start the browser for the report reader: {e}"
        try:
            # errors="replace": a single malformed byte must not blank the whole result.
            # Strict decoding fails on the reader thread and silently yields stdout=None.
            proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                                  encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            if checkpoint.exists():
                partial = json.loads(checkpoint.read_text(encoding="utf-8"))
                partial["coverage"] = dict(partial.get("coverage", {}), complete=False, interrupted=True,
                                          warning="Timeout; this is only the saved partial scan.")
                self.atomic_json(checkpoint, partial)
                if mode == "summaries":
                    return partial, None
            return None, f"Report reader timed out; partial work saved at {checkpoint}. Resume with the SAME scope; successful PDFs are reused, not downloaded again."
        except Exception as e:
            return None, f"Failed to launch report reader: {e}"
        if proc.returncode != 0:
            detail = (proc.stderr or "").strip()
            if len(detail) > 1500:
                detail = detail[:250] + "\n...[traceback middle omitted]...\n" + detail[-1200:]
            return None, f"Report reader failed (exit {proc.returncode}); saved partial work: {checkpoint}. Resume the same scope.\n{detail}"
        try:
            return json.loads(proc.stdout), None
        except Exception:
            return None, f"Report reader returned unparseable output:\n{(proc.stdout or '').strip()[:1000]}"

    def run_turn_anthropic(self, messages: list, chat_id: int) -> str:
        return turn_loop.run(self, messages, chat_id, turn_loop.AnthropicMessages(self, chat_id))

    def _cache_key(self):
        """One key per conversation: OpenAI routes requests with the same prompt_cache_key to the same cache node.
        Measured 2026-10-08: 19% cache reads over 4M input tokens on the ChatGPT route without it."""
        return f"{self.AGENT_DIR.name}-{self.BACKGROUND_TASK_ID or 'chat'}"


    def tool_log_activity(self, note: str) -> str:
        return WorkingMemory(self.AGENT_DIR).activity(note)

    def tool_remember(self, kind: str = "", content: str = "", source: str = "", quote: str = "", valid_from: str = "",
                      valid_to: str = "", supersedes: str = "", old: str = "", new: str = "", reason: str = "",
                      name: str = "", state: str = "", done: bool = False, pinned: bool = False,
                      owner_asked: bool = False, method_path: str = "", chat_id: int = None) -> str:
        """One memory writer (2026-10-08): a sourced fact, an unsourced note, an exact correction, or a working
        thread. Replaces record_fact / append_memory / reconcile_memory / work_thread, which overlapped and were
        picked by the shape of the sentence rather than by rule; delete_memory_section is now a maintenance script."""
        if kind == "lesson":
            # The lesson and its edit are one record (2026-11-01: 183 candidates, 40 drafts nobody showed Alex).
            if not method_path or not (new or "").strip():
                return Failed("A lesson is recorded together with its edit, in one step: method_path (the skill file under "
                              "skills/, or SKILL.md for a habit of conversation), old (the exact current text to sharpen, "
                              "or empty to add a ledger entry) and new (the new text in the skill's own voice). Without "
                              "the edit nothing is recorded.")
            try:
                shown = record_lesson(self.AGENT_DIR, chat_id, content, source or "foreground observation", method_path, old, new)
            except ValueError as exc:
                return Failed(str(exc))
            return ("Lesson registered for Alex's go as " + shown["key"] + ". It reaches him on the dashboard and in the "
                    "weekly message, so send it only if he asks. Nothing changes until he approves it.\n" + shown["show_owner"])
        if old or new:
            if not (old and new):
                return "A correction needs both old (exact, occurring once) and new."
            return reconcile(self.MEMORY_FILE, old, new, reason or "Corrected through remember")
        if kind == "thread":
            return self.tool_work_thread(name, state, done, pinned, owner_asked)
        if kind == "consolidate":
            from tabularium.memory import memory_consolidation as mc
            try:
                if old:
                    n = mc.propose(self.AGENT_DIR, old, new, reason)
                    return f"Edit {n} added to the memory candidate (not live). Add more, or state=trial to run the exam."
                if state == "trial":
                    if not mc.status(self.AGENT_DIR):
                        return Failed("No candidate: propose at least one edit first.")
                    (self.AGENT_DIR / "task_state" / "memory_trial_requested").touch()
                    return ("Trial queued: within a minute the memory exam runs on the live memory and on the candidate "
                            "(about two minutes). Then state=status shows both scores; show Alex the scores and the edits.")
                if state == "apply":
                    done = mc.apply(self.AGENT_DIR)
                    return f"Applied {done['edits']} edit(s) to memory.md; the old text is backed up."
                if state == "discard":
                    mc.discard(self.AGENT_DIR); return "Memory candidate discarded."
                if state == "rebase":
                    n = mc.rebase(self.AGENT_DIR)
                    return f"Replayed {n} edit(s) on the current memory.md. Run state=trial again before showing Alex."
                return mc.summary(self.AGENT_DIR) + ("\n\n" + mc.diff(self.AGENT_DIR) if mc.status(self.AGENT_DIR) else "")
            except ValueError as exc:
                return Failed(str(exc))
        if kind == "note":
            if not content.strip():
                return "A note needs content."
            return append_unique(self.MEMORY_FILE, f"- {datetime.now().strftime('%Y-%m-%d')} (unsourced note): {content.strip()}")
        if kind in ("user_preference", "user_decision", "assistant_proposal", "observation", "inference"):
            try:
                self._recall.ingest_saved(chat_id)
                return record_fact(self.MEMORY_FILE, self._recall, chat_id, content, kind, source, quote, valid_from, valid_to, supersedes)
            except (ValueError, OSError) as exc:
                return "Fact not saved: " + str(exc)
        return ("remember needs kind: user_decision, user_preference, assistant_proposal, observation, inference (sourced), "
                "note (unsourced), or thread; or old+new for a correction.")

    def tool_work_thread(self, name: str, state: str = "", done: bool = False,
                         pinned: bool = False, owner_asked: bool = False) -> str:
        return WorkingMemory(self.AGENT_DIR).thread(name, state, done, pinned, owner_asked)

    def _load_core_memory(self) -> str:
        return current_context(self.AGENT_DIR, include_working=not self.BACKGROUND_TASK_ID)
