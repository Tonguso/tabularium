"""An agent is a folder of data; this builds the running object from it.

The folder holds what is that agent's own: SKILL.md (its identity), memory.md, skills/, routing.json and its records.
Which code runs it is decided by the roster: each slot names a role module here (coordinator, librarian, engineer,
private), and the module's CONFIG is the role's default configuration. An optional agent.toml in the folder overrides
any field of that configuration (the display name, the prompts, ports, the voice), so the persona lives with the
agent and the mechanism lives in the package. Unknown keys are refused: a misspelt setting must not pass silently.
"""
import dataclasses
import importlib
import importlib.util
import tomllib
from pathlib import Path

from tabularium.core.roster import ROLES


def overrides(agent_dir) -> dict:
    path = Path(agent_dir) / "agent.toml"
    return tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def configure(default, values: dict):
    """The role's default Config with the agent's own values applied, each coerced to the field's type."""
    fields = {f.name: f for f in dataclasses.fields(default)}
    unknown = sorted(set(values) - set(fields))
    if unknown:
        raise ValueError("agent.toml: unknown setting(s) " + ", ".join(unknown))
    if "role" in values or "name" in values:
        raise ValueError("agent.toml: the agent's name and role come from the roster, not the file")
    changes = {}
    for key, value in values.items():
        current = getattr(default, key)
        if isinstance(current, tuple):
            value = tuple(tuple(v) if isinstance(v, list) else v for v in value)
        elif isinstance(current, frozenset):
            value = frozenset(value)
        elif isinstance(current, dict):
            value = dict(current, **value)
        elif isinstance(current, bool) and not isinstance(value, bool) or \
                isinstance(current, int) and not isinstance(current, bool) and not isinstance(value, int) or \
                isinstance(current, str) and not isinstance(value, str):
            raise ValueError(f"agent.toml: {key} must be {type(current).__name__}, not {type(value).__name__}")
        changes[key] = value
    return dataclasses.replace(default, **changes)


class ModuleAgent:
    """A module that *is* the agent: its own names first, then its `runtime`; assignments go to the runtime. The form
    the entry-point files had until 2026-11-08 (module-level helpers over a runtime object)."""

    def __init__(self, module):
        object.__setattr__(self, "_module", module)

    def __getattr__(self, name):
        module = object.__getattribute__(self, "_module")
        return getattr(module, name) if hasattr(module, name) else getattr(module.runtime, name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_module").runtime, name, value)


def build(agent_dir, name=None):
    """The agent for this folder: the role module's App over the role's Config with the folder's agent.toml applied.
    Reads the environment (its bot token, its background-task id) as the entry points always did."""
    agent_dir = Path(agent_dir).resolve()
    name = name or agent_dir.name
    if name not in ROLES:
        raise ValueError(f"Unknown agent slot {name!r}; the roster has {', '.join(ROLES)}")
    own = agent_dir / "agent.py"
    if own.exists():
        # A bespoke agent: the folder carries its own role module. It defines build(agent_dir, overrides) or a
        # ready `runtime`; the slot still decides its records' shape. The test suite's scripted workers use this.
        spec = importlib.util.spec_from_file_location(f"tabularium_agent_{name}", own)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if hasattr(module, "build"):
            return module.build(agent_dir, overrides(agent_dir))
        return ModuleAgent(module)
    module = importlib.import_module(f"tabularium.agents.{ROLES[name].entry}")
    return module.build(agent_dir, overrides(agent_dir))
