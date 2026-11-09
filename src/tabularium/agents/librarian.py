"""
Callimachus -- Telegram Interface

Run:
    tabularium run callimachus

The message path lives in tabularium/core/agent_app.py (one class for all four agents, 2026-10-11). This file holds what is
Cal's alone: the knowledge tool, his write scope, and a librarian's prompt.
"""


import json
import os
from pathlib import Path

from tabularium.tools.knowledge import TOOL as KNOWLEDGE_TOOL
from tabularium.core.agent_app import AgentApp, Config
from tabularium.core.roster import ROLES

CONFIG = Config(
    name="callimachus", role=ROLES["callimachus"], display="Callimachus",
    token_env="CALLIMACHUS_TELEGRAM_BOT_TOKEN",
    token_missing="CALLIMACHUS_TELEGRAM_BOT_TOKEN not set. Add it to .env; never reuse another agent token.",
    shared_token_envs=("TELEGRAM_BOT_TOKEN", "TACITUS_TELEGRAM_BOT_TOKEN"),
    token_refusal="Refusing to poll an existing agent's Telegram bot. Configure Cal's own token.",
    provider_env="CALLIMACHUS_MODEL_PROVIDER", provider_default="openai",
    instance_port=8215, cdp_port=9224,
    voice_env="CALLIMACHUS_ELEVENLABS_VOICE_ID",
    tools_after=(("list_images", KNOWLEDGE_TOOL),),
    tools_remove=frozenset({"project_record"}),      # Cal keeps task progress locally; Cato owns the cross-project register.
    sanitise="code_safe",
    expire_module="tabularium.delegation.research_client",
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
    character_prompt="""- You are Callimachus, normally Cal: Alex's librarian and knowledge steward, never Cato.
- Quiet scholar-investigator: courteous scepticism, precise evidence, warmth and dry wit when apt.
- Use Alex's current language with judgment; simplified Chinese; always write his name as Alex.
- Do not import Cato's JARVIS persona or habitual sir. Challenge material claims using actual evidence.
""",
    ops_slots=dict(
        recurring="""- For recurring retrieval, use the approved source scope and the knowledge-stewardship skill.
  Cato owns the cross-project register; link to originals rather than creating a competing register.""",
        advisor="  Only send relevant private/work evidence to an advisor under applicable authorization for this task.",
        records="""- Keep librarian-owned dossiers only for explicitly assigned subjects. Use knowledge records with
  dated sources, counterevidence, open questions and revision history."""),
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
        "Photo    — send an image for Callimachus to analyse\n"
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


class CallimachusApp(AgentApp):
    def dispatch_extra(self, name, inputs, chat_id):
        if name == "knowledge":
            from tabularium.tools.knowledge import dispatch
            try:
                return self._cap_result(json.dumps(dispatch(**inputs), ensure_ascii=False))
            except (ValueError, OSError, KeyError, TypeError) as exc:
                return "Archive operation not completed: " + str(exc)
        if name == "project_record":
            return "Cato owns the shared project/decision register. Use sourced knowledge records, not a competing register."
        if name in ("write_file", "edit_file"):
            # Direct file tools may write inside Cal's workspace and the knowledge base wiki
            # (Alex, 2026-10-06: [quote removed]). Library originals stay read-only.
            path = Path(inputs.get("path", inputs.get("file_path", ""))).resolve()
            wiki_root = Path(os.environ.get("KNOWLEDGE_BASE_DIR", r"D:\KnowledgeBase\_wiki"))
            if not (path.is_relative_to(self.AGENT_DIR.resolve()) or path.is_relative_to(wiki_root)):
                return ("Cal's direct file writes are restricted to his workspace and the knowledge-base wiki folder (KNOWLEDGE_BASE_DIR). "
                        "Library originals are read-only. Ask Alex/Cato to handle other external changes.")
        return None

def build(agent_dir, overrides=None):
    """The callimachus agent for this folder (see tabularium.agents.build)."""
    from tabularium.agents import configure
    return CallimachusApp(configure(CONFIG, overrides or {}), agent_dir)
