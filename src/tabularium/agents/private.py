"""
Tacitus -- Telegram Interface

Run:
    tabularium run private

The message path lives in tabularium/core/agent_app.py (one class for all agents). This file holds what is Tacitus's
alone: a role-scoped tool set, a voice, and a private companion's prompt. Tacitus is the example of the roster's
private role: no shell, no delegation, no learning loop, the smallest tool set, and records that maintainers never
read. Replace the standing method (skills/standing-method/SKILL.md) with the method you want followed on every turn.
"""


from pathlib import Path

from tabularium.core.agent_app import AgentApp, Config
from tabularium.core.roster import ROLES

PRIVATE_ALLOWED_TOOLS = frozenset({'verify_requirement', 'task_decision', 'record_fact', 'task_checkpoint', 'read_memory', 'background_task', 'edit_file', 'view_image', 'reconcile_memory', 'skill', 'web_search', 'list_images', 'search_conversations', 'read_conversation_excerpt', 'delete_memory_section', 'write_file', 'append_memory', 'speak', 'task_evidence', 'send_document', 'read_file', 'consult_advisor', 'web_fetch', 'remember'})

CONFIG = Config(
    name="private", role=ROLES["private"], display="Tacitus",
    token_env="PRIVATE_TELEGRAM_BOT_TOKEN", token_missing="PRIVATE_TELEGRAM_BOT_TOKEN not set.",
    shared_token_envs=("TELEGRAM_BOT_TOKEN", "LIBRARIAN_TELEGRAM_BOT_TOKEN", "ENGINEER_TELEGRAM_BOT_TOKEN"),
    token_refusal="Tacitus requires his own Telegram token.", token_refusal_style="raise",
    provider_env="PRIVATE_MODEL_PROVIDER", provider_default="anthropic",
    instance_port=8213, cdp_port=9228,
    activity_interval=8,
    reasoning_effort="low",
    window=(100, 140),
    always_skills=("standing-method",),   # loaded on every turn as a standing instruction, not on demand
    prefs_model_key=True,
    start_reply="Here when you need me.",
    threads_stub="Use /tasks for requested background work. Our conversation does not require task tracking.",
    auto_stub="Tacitus has no shell tools or command auto-mode.",
    tts="openai", tts_voice="sage",
    tts_instructions="Speak warmly, calmly, and unhurried. Soft-spoken and measured, with genuine care. Never rushed.",
    outlook_browser_tools=False,
    tool_allow=PRIVATE_ALLOWED_TOOLS,
    plan_signin_aware=True,
    telegram_prompt="""
## Telegram
Keep replies warm, natural and conversational with minimal formatting. Preserve literal quotes and technical text. Use the owner's language with judgment. Conversation is ongoing, not a series of sessions.
""",
    character_prompt="""
## Runtime reliability
Your identity is defined above. Memory stays in your existing single file, fully loaded and refreshed on subsequent turns. Use precise append/reconcile operations; preserve history. Retrieve relevant saved conversation excerpts and images rather than guessing. Recalled beliefs are not verified facts.
Only explicitly requested substantial work belongs in background tasks; ordinary conversation stays foreground. Save evidence, next steps and blockers. Stop means stop. Never replay an external action with unknown outcome. Use task names in conversation, IDs only internally.
Tools are role-scoped: no shell, email, deployment, image generation or other agents' credentials. Never copy other agents' memories, histories or sign-ins. Advisor authority does not extend to arbitrary sharing of sensitive disclosures.
Use voice for conversational voice replies and text for precision or visual content. Never invent access, progress or completion. The dashboard is management-only.
""",
    worker_note="\nYou are the background worker. Execute the assigned task here; do not delegate recursively. Preserve checkpoints and end with findings, artifacts, evidence and any remaining work.",
    core_memory_detail="(notes and dated history)",
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
        "/voice   — voice replies: on · off · auto\n"
        "Voice    - send a voice note (transcribed)\n"
        "Photo    — send an image for Tacitus to look at\n"
        "File     — send .docx / .txt / .pdf"
    ),
    checkpoint_note=("\n\n[Silent checkpoint — if the recent conversation covered anything worth "
                     "remembering (a decision, a result, an open thread), call append_memory now "
                     "without duplicating memory. If nothing notable, ignore this. Either way, do NOT "
                     "mention this checkpoint, do not say you logged anything, and do not let it "
                     "change your reply — just answer normally.]"),
    nightly_save_prompt=(
        "It is now time for the nightly save. Review this conversation and save only new facts or exact corrections with remember. "
        "Be thorough. You must actually call remember — do not just describe what you would save."
    ),
)


class TacitusApp(AgentApp):
    def dispatch_extra(self, name, inputs, chat_id):
        if name not in PRIVATE_ALLOWED_TOOLS:
            return "That tool is outside Tacitus's role."
        if name == "write_memory":
            return "Whole-file memory replacement is disabled. Use remember with an exact correction or sourced fact."
        if name == "remember":
            return self.tool_remember(chat_id=chat_id, **inputs)
        if name in ("read_file", "write_file", "edit_file", "view_image", "send_document"):
            path = Path(inputs.get("path", inputs.get("file_path", ""))).resolve()
            if path.name.lower() in ("config.bat", ".env", "auth.json", "openai_chatgpt.dat"):
                return "Credential files are not model-readable."
            if name in ("write_file", "edit_file") and not path.is_relative_to(self.AGENT_DIR.resolve()):
                return "Tacitus's writes are limited to his own workspace."
        return None

def build(agent_dir, overrides=None):
    """The private agent for this folder (see tabularium.agents.build)."""
    from tabularium.agents import configure
    return TacitusApp(configure(CONFIG, overrides or {}), agent_dir)
