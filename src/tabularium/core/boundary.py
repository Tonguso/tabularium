"""One boundary for every tool that takes a path (2026-11-12).

An agent reads and writes inside its own folder and the work roots the owner declared for it in agent.toml
(work_roots); never another agent's folder, never the home's own files (.env, POLICY.md, the dashboard's state).
Inside its own folder the file tools do not write memory.md (the memory tool does, with a quote) or a skill (a skill
changes only on the owner's word). Until today read_file and write_file took any path: the private agent's memory was
readable by the others, and a turn that had read outside content could write the PowerShell profile that every later command loaded.
"""
from pathlib import Path
from tabularium.core.outcome import Refused

PATH_INPUTS = ("path", "file_path", "paths")
WRITERS = {"write_file", "edit_file"}


def _resolve(agent, value):
    p = Path(str(value))
    if not p.is_absolute():
        p = Path(agent.AGENT_DIR) / p
    return p.resolve()


def refusal(agent, value, write=False):
    """Refused(...) when `value` lies outside the agent's boundary; None when the path may be used."""
    own = Path(agent.AGENT_DIR).resolve()
    home = own.parent
    try:
        p = _resolve(agent, value)
    except (OSError, ValueError):
        return Refused(f"Not a usable path: {value}")
    if p == own or p.is_relative_to(own):
        if write and (p == own / "memory.md" or p == own / "SKILL.md" or p.is_relative_to(own / "skills")):
            return Refused("Memory changes go through the memory tool; a skill changes only on Alex's approval of a lesson.")
        return None
    roots = [Path(r).resolve() for r in getattr(agent.config, "work_roots", ())]
    if any(p == r or p.is_relative_to(r) for r in roots) and not (p == home or p.is_relative_to(home)):
        return None
    shown = ", ".join(str(r) for r in roots) or "none declared"
    return Refused(f"Outside this agent's boundary: {p}. It may use its own folder ({own}) and its work roots ({shown}); "
                   "never another agent's folder or the home's own files. Ask Alex to add a work root in agent.toml if the work needs it.")


def check(agent, name, inputs):
    """The guard execute_tool runs for every tool: each path input checked, write tools checked as writes."""
    for key in PATH_INPUTS:
        value = inputs.get(key)
        if not value:
            continue
        for item in (value if isinstance(value, (list, tuple)) else [value]):
            held = refusal(agent, item, write=name in WRITERS)
            if held:
                return held
    return None
