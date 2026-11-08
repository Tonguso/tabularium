"""Where an installation lives: code in the package, state in the home folder.

The home folder holds everything that is the owner's: one folder per agent (its identity, memory, skills and records),
the configuration (.env, POLICY.md), the logs and the dashboard's state files. It is TABULARIUM_HOME when that is set,
otherwise the current directory; the command-line entry point sets the variable before anything is imported, and
passes it to every child process, so one installation never reads another's records.
"""
import os
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent


def home() -> Path:
    return Path(os.environ.get("TABULARIUM_HOME") or os.getcwd()).resolve()


def agent_dir(name) -> Path:
    return home() / name


def logs() -> Path:
    return home() / "logs"


def child_env(**extra) -> dict:
    """The environment for a process this installation starts: the same home, and the package importable whether
    or not it is installed (its parent folder leads PYTHONPATH), plus whatever the caller adds."""
    env = dict(os.environ, TABULARIUM_HOME=str(home()), **{k: str(v) for k, v in extra.items()})
    src = str(PACKAGE.parent)
    current = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p and p != src]
    env["PYTHONPATH"] = os.pathsep.join([src, *current])
    return env
