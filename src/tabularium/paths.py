"""Where an installation lives: code in the package, state in the home folder.

The home folder holds everything that is the owner's: one folder per agent (its identity, memory, skills and records),
the configuration (.env, POLICY.md), the logs and the dashboard's state files. It is TABULARIUM_HOME when that is set,
otherwise the current directory; the command-line entry point sets the variable before anything is imported, and
passes it to every child process, so one installation never reads another's records.
"""
import os
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent


def home() -> Path:
    return Path(os.environ.get("TABULARIUM_HOME") or os.getcwd()).resolve()


def agent_dir(name) -> Path:
    return home() / name


def logs() -> Path:
    return home() / "logs"


def child_env(home_dir=None, **extra) -> dict:
    """The environment for a process this installation starts: the same home, the package importable whether or
    not it is installed (its parent folder leads PYTHONPATH), the interpreter's own Scripts folder on PATH so the
    tools installed in its environment can be run, plus whatever the caller adds. `home_dir` names another
    installation for a child that must not read this one (an isolated evaluation run)."""
    env = dict(os.environ, **{k: str(v) for k, v in extra.items()})
    env["TABULARIUM_HOME"] = str(Path(home_dir).resolve() if home_dir else home())
    src = str(PACKAGE.parent)
    current = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p and p != src]
    env["PYTHONPATH"] = os.pathsep.join([src, *current])
    scripts = str(Path(sys.executable).resolve().parent / "Scripts")
    if os.path.isdir(scripts) and scripts.lower() not in env.get("PATH", "").lower():
        env["PATH"] = os.pathsep.join([scripts, env.get("PATH", "")])
    return env
