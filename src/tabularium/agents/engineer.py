"""
Vitruvius -- Telegram Interface

Run:
    tabularium run vitruvius

The message path lives in tabularium/core/agent_app.py (one class for all four agents, 2026-10-11). This file holds what is
Vit's alone: the run_checks tool and an engineer's prompt.
"""


import json
import re
import subprocess
import sys
import time
from pathlib import Path

from tabularium.core.agent_app import AgentApp, Config
from tabularium.core.command_output import command_result
from tabularium.core.roster import ROLES
from tabularium.core.outcome import Failed

CHECK_TIMEOUT_DEFAULT = 300

RUN_CHECKS_TOOL = {"name": "run_checks", "description": "Run tests, lint or type checks with a long timeout and get a structured verdict. kinds: unittest (python -m unittest discover -s target), pytest (target = dir/file/-k expr), ruff (lint target), mypy (type-check target), compile (python -m compileall target), npm_test (npm test in cwd). Up to 600 s; verbose output archived with diagnostic excerpts. Use this instead of execute_command for any check: it reports exit code, duration and the summary line, and a timeout kills the whole tree.",
     "input_schema": {"type": "object", "properties": {
         "kind": {"type": "string", "enum": ["unittest", "pytest", "ruff", "mypy", "compile", "npm_test"]},
         "cwd": {"type": "string", "description": "Working directory (absolute)."},
         "target": {"type": "string", "description": "Directory, file or pattern the check runs on; defaults to cwd."},
         "args": {"type": "string", "description": "Extra arguments, space separated."},
         "timeout": {"type": "integer", "minimum": 10, "maximum": 600, "description": "Seconds; default 300."}},
      "required": ["kind", "cwd"]}}

CONFIG = Config(
    name="vitruvius", role=ROLES["vitruvius"], display="Vitruvius",
    token_env="VITRUVIUS_TELEGRAM_BOT_TOKEN",
    token_missing="VITRUVIUS_TELEGRAM_BOT_TOKEN not set. Add it to .env; never reuse another agent token.",
    shared_token_envs=("TELEGRAM_BOT_TOKEN", "TACITUS_TELEGRAM_BOT_TOKEN", "CALLIMACHUS_TELEGRAM_BOT_TOKEN"),
    token_refusal="Refusing to poll an existing agent's Telegram bot. Configure Vit's own token.",
    provider_env="VITRUVIUS_MODEL_PROVIDER", provider_default="openai",
    instance_port=8216, cdp_port=9226,
    voice_env="VITRUVIUS_ELEVENLABS_VOICE_ID",
    tools_after=(("read_research_reports", RUN_CHECKS_TOOL),),
    tools_remove=frozenset({"sec_filing"}),        # filings research is Cato's and Cal's
    sanitise="code_safe",
    expire_module="tabularium.delegation.work_client",
    telegram_prompt="""

## Telegram Formatting

You are running inside a Telegram bot. Keep responses concise and conversational.
- Write mostly in plain prose. Use **bold** or *italics* sparingly — only to emphasise a genuinely
  key term or number. Most messages need none. Do not decorate or emphasise out of habit.
- No headers, horizontal rules, or tables.
- For lists, use plain numbering (1. 2. 3.) or write inline.
- Telegram has no math rendering: write formulas in plain text / Unicode (Σ, ×, ÷, √, subscripts
  like shares_pr). Underscores in notation render literally, so use them freely for variables.

## Email
Use POLICY.md for authorization; list available sender accounts when needed.
""",
    character_prompt="""- You are Vitruvius, normally Vit, Alex's engineer and web designer. Composed, exacting,
  resourceful and direct; strong opinions revised by evidence. No theatrical genius or flattery.
- Speak natural English or simplified Chinese with sentence-appropriate punctuation. Write Alex.
  Use sir occasionally when natural. Never invent progress, test results or completion.
""",
    ops_slots=dict(
        recurring="""- For substantial implementation, use the engineering-workflow skill and explicit acceptance criteria.
  Keep engineering task records scoped; Cato owns priorities and acceptance.""",
        advisor="  Only send relevant private/work evidence to an advisor under applicable authorization for this task.",
        records="- For engineering work preserve requirements, scope, change evidence, tests and unresolved issues in task checkpoints."),
    worker_note="\nYou are the background worker. Execute the assigned task here; do not delegate recursively. Preserve checkpoints and end with findings, artifacts, evidence and any remaining work.",
    core_memory_detail="(project specifics like current methodologies, tools, infrastructure)",
    help_text=(
        "/tasks — background status\n"
        "/background <task> — start background work\n"
        "/steer \"task name\" <instruction> · /cancel <task name> · /resume <task name>\n"
        "/start   — reset conversation, reload memory\n"
        "/clear   — clear conversation (memory intact)\n"
        "/reload  — reload memory into current session\n"
        "/memory  — view current memory file\n"
        "/model   — show current model\n"
        "/model <name> — switch model\n"
        "           haiku · sonnet · opus · fable\n"
        "           opus 4.8 · opus 4.7 · sonnet 4.6 · etc.\n"
        "           openai · local\n"
        "/voice   — voice replies: on · off · auto\n"
        "/auto    — on: run everything · off: confirm only risky commands (default)\n"
        "/openai  - subscription first, GPT-6.1 API fallback\n"
        "Voice    - send a voice note (transcribed via Whisper)\n"
        "Photo    — send an image for Vitruvius to analyse\n"
        "File     — send .docx / .txt / .pdf"
    ),
    checkpoint_note=("\n\n[Silent checkpoint — if the recent conversation covered anything worth "
                     "remembering (a decision, a result, an open thread), call log_activity now "
                     "with one concise line. If nothing notable, ignore this. Either way, do NOT "
                     "mention this checkpoint, do not say you logged anything, and do not let it "
                     "change your reply — just answer Alex normally.]"),
    nightly_save_prompt=(
        "It is now time for the nightly save. Review this entire conversation and save everything "
        "worth remembering to your memory file — decisions made, important context, anything "
        "Alex would want you to recall in the next session. Be thorough."
    ),
)


class VitruviusApp(AgentApp):
    def dispatch_extra(self, name, inputs, chat_id):
        if name == "run_checks":
            return self.tool_run_checks(**inputs)
        return None

    def tool_run_checks(self, kind: str, cwd: str, target: str = "", args: str = "", timeout: int = CHECK_TIMEOUT_DEFAULT) -> str:
        """Tests/lint/type checks with a real timeout and a structured result. execute_command's 60 s limit
        forced Vit to background every test run by hand (2026-10-07 upgrade)."""
        import shlex
        base = Path(cwd).resolve()
        if not base.is_dir():
            return Failed(f"Error: cwd {cwd} is not a directory.")
        candidate = (base / target) if target else base
        tgt = str(candidate.resolve()) if candidate.exists() else target
        extra = shlex.split(args or "", posix=False)
        py = sys.executable
        commands = {
            "unittest": [py, "-X", "utf8", "-m", "unittest", "discover", "-s", tgt, "-v"],
            "pytest": [py, "-X", "utf8", "-m", "pytest", "-q", tgt],
            "ruff": [py, "-m", "ruff", "check", tgt],
            "mypy": [py, "-m", "mypy", "--pretty", tgt],
            "compile": [py, "-m", "compileall", "-q", tgt],
            "npm_test": ["cmd", "/c", "npm", "test"],
        }
        if kind not in commands:
            return Failed(f"Error: unknown kind {kind}. Use one of {', '.join(commands)}.")
        argv = commands[kind] + extra
        limit = max(10, min(int(timeout or CHECK_TIMEOUT_DEFAULT), 600))
        started = time.time()
        proc = subprocess.Popen(argv, cwd=str(base), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            out, _ = proc.communicate(timeout=limit)
            timed_out = False
        except subprocess.TimeoutExpired:
            self._kill_tree(proc.pid)
            out, _ = proc.communicate(timeout=10)
            timed_out = True
        text = (out or b"").decode("utf-8", errors="replace")
        lines = [l for l in text.splitlines() if l.strip()]
        # The verdict line, not log noise: unittest's OK/FAILED/Ran N, pytest's "N passed", ruff/mypy totals.
        verdict = re.compile(r"^(OK\b|FAILED\b|Ran \d+ tests?\b|=+ .*(passed|failed|error).* =+$|\d+ passed\b|\d+ failed\b|Found \d+ error|All checks passed|Success: no issues|Found \d+ errors? in)")
        summary = next((l.strip() for l in reversed(lines) if verdict.search(l.strip())), (lines[-1] if lines else ""))
        result = json.loads(command_result(text, "", proc.returncode,
                                          self.RUNTIME_DIR / "context_archive" / "results", timed_out=timed_out))
        result.update(kind=kind, command=" ".join(argv), cwd=str(base),
                      duration_s=round(time.time() - started, 1), summary=summary[:300])
        encoded = json.dumps(result, ensure_ascii=False)
        return encoded if result["passed"] else Failed(encoded)

def build(agent_dir, overrides=None):
    """The vitruvius agent for this folder (see tabularium.agents.build)."""
    import os
    for limit in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
        os.environ[limit] = "1"
    from tabularium.agents import configure
    return VitruviusApp(configure(CONFIG, overrides or {}), agent_dir)
