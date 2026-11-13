"""Build a real agent for a test, in a temporary folder.

The agent is the production object (engine.AgentRuntime -> SharedHandlers -> AgentApp -> the role's App) built by
tabularium.agents.build from a temporary agent folder, exactly as `tabularium run` and the background worker build it.
Tests replace only what touches the outside world (the model call, Telegram, subprocesses) on that object. Until
2026-10-14 many tests lifted single functions out of the flattened source into hand-made scopes; they tested a fake
and broke on every refactor, so they were rewritten on this harness and the flattening was removed.
"""
import os
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ("coordinator", "private", "librarian", "engineer")
STYLE = Path("skills/talk-like-a-human-being/runtime-core.md")


def load_agent(agent, root):
    """{"runtime": the agent}, built as background worker 1 so it never claims the live agent's Telegram port.
    make_agent() is the usual entry point."""
    root = Path(root)
    (root / "SKILL.md").write_text("# Test identity\nAnswer briefly.\n", encoding="utf-8")
    (root / "memory.md").write_text("# Memory\n\n## Core\n- test\n", encoding="utf-8")
    if (ROOT / agent / STYLE).exists():
        (root / STYLE).parent.mkdir(parents=True, exist_ok=True)
        (root / STYLE).write_bytes((ROOT / agent / STYLE).read_bytes())
    (root / "task_state" / "background" / "1").mkdir(parents=True, exist_ok=True)
    from tabularium.records import background_runtime                 # the worker namespace expects its task row to exist
    store = background_runtime.TaskStore(root)
    with store.db() as db:
        db.execute("INSERT OR IGNORE INTO tasks(id,chat,request,status,created,updated) VALUES (1,1,'self-test','running',0,0)")
    env = {"ANTHROPIC_API_KEY": "test", "OPENAI_API_KEY": "test", "SEARCH_PROVIDER": "anthropic",
           f"{agent.upper()}_BACKGROUND_TASK": "1", f"{agent.upper()}_AGENTMAIL_API_KEY": "am_test", "TELEGRAM_BOT_TOKEN": "1:a", "PRIVATE_TELEGRAM_BOT_TOKEN": "2:b",
           "LIBRARIAN_TELEGRAM_BOT_TOKEN": "3:c", "ENGINEER_TELEGRAM_BOT_TOKEN": "4:d"}
    from tabularium.agents import build
    with patch.dict(os.environ, env):
        runtime = build(root, agent)
    return {"runtime": runtime}


def make_agent(agent, root, background=False):
    """A real agent in `root`. background=False makes it the foreground agent (the usual case); True keeps it as
    background worker 1, whose task row exists."""
    runtime = load_agent(agent, root)["runtime"]
    runtime.BACKGROUND_TASK_ID = "1" if background else ""
    return runtime


def put(store, ident, status, note=""):
    """Test fixture only: place a task in `status` directly, to set up a situation. Production code changes status
    only through tabularium.records.lifecycle operations (TaskStore.move); a test of a transition must use those."""
    import time
    with store.db() as db:
        db.execute("UPDATE tasks SET status=?,note=?,updated=?,notified=0 WHERE id=?", (status, note, time.time(), int(ident)))
        if status == "interrupted":
            db.execute("INSERT OR IGNORE INTO worker_failures(task,note,created) VALUES (?,?,?)", (int(ident), note, time.time()))
