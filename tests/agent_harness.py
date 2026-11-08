"""Build a real agent for a test, in a temporary folder.

The agent is the production object (engine.AgentRuntime -> SharedHandlers -> AgentApp -> the agent's subclass) loaded
from its own telegram.py with AGENT_DIR pointed at the temporary folder. Tests replace only what touches the outside
world (the model call, Telegram, subprocesses) on that object. Until 2026-10-14 many tests lifted single functions
out of the flattened source into hand-made scopes (agent_source.merged_source); they tested a fake and broke on every
refactor, so they were rewritten on this harness and the flattening was removed.
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
AGENTS = ("cato", "tacitus", "callimachus", "vitruvius")


def load_agent(agent, root):
    """The agent's module namespace (scope["runtime"] is the agent), loaded as background worker 1 so it never claims
    the live agent's Telegram port. make_agent() is the usual entry point."""
    root = Path(root)
    source = (ROOT / agent / "telegram.py").read_text(encoding="utf-8")
    assert "AGENT_DIR   = Path(__file__).resolve().parent" in source, agent
    source = source.replace("AGENT_DIR   = Path(__file__).resolve().parent", "AGENT_DIR   = Path(" + repr(str(root)) + ")")
    (root / "SKILL.md").write_text("# Test identity\nAnswer briefly.\n", encoding="utf-8")
    (root / "memory.md").write_text("# Memory\n\n## Core\n- test\n", encoding="utf-8")
    style = Path("skills/talk-like-a-human-being/runtime-core.md")
    if (ROOT / agent / style).exists():
        (root / style).parent.mkdir(parents=True, exist_ok=True)
        (root / style).write_bytes((ROOT / agent / style).read_bytes())
    (root / "task_state" / "background" / "1").mkdir(parents=True, exist_ok=True)
    import background_runtime                                    # the worker namespace expects its task row to exist
    store = background_runtime.TaskStore(root)
    with store.db() as db:
        db.execute("INSERT OR IGNORE INTO tasks(id,chat,request,status,created,updated) VALUES (1,1,'self-test','running',0,0)")
    env = {"ANTHROPIC_API_KEY": "test", "OPENAI_API_KEY": "test", "SEARCH_PROVIDER": "anthropic",
           f"{agent.upper()}_BACKGROUND_TASK": "1", "TELEGRAM_BOT_TOKEN": "1:a", "TACITUS_TELEGRAM_BOT_TOKEN": "2:b",
           "CALLIMACHUS_TELEGRAM_BOT_TOKEN": "3:c", "VITRUVIUS_TELEGRAM_BOT_TOKEN": "4:d"}
    scope = {"__file__": str(ROOT / agent / "telegram.py"), "__name__": f"isolated_{agent}"}
    with patch.dict(os.environ, env):
        sys.modules.pop("engine", None)
        for p in (str(ROOT / agent), str(ROOT)):
            if p in sys.path:
                sys.path.remove(p)
        sys.path[:0] = [str(ROOT / agent), str(ROOT)]
        exec(compile(source, str(ROOT / agent / "telegram.py"), "exec"), scope)
    return scope


def make_agent(agent, root, background=False):
    """A real agent in `root`. background=False makes it the foreground agent (the usual case); True keeps it as
    background worker 1, whose task row exists."""
    runtime = load_agent(agent, root)["runtime"]
    runtime.BACKGROUND_TASK_ID = "1" if background else ""
    return runtime


def put(store, ident, status, note=""):
    """Test fixture only: place a task in `status` directly, to set up a situation. Production code changes status
    only through agent_core.lifecycle operations (TaskStore.move); a test of a transition must use those."""
    import time
    with store.db() as db:
        db.execute("UPDATE tasks SET status=?,note=?,updated=?,notified=0 WHERE id=?", (status, note, time.time(), int(ident)))
        if status == "interrupted":
            db.execute("INSERT OR IGNORE INTO worker_failures(task,note,created) VALUES (?,?,?)", (int(ident), note, time.time()))
