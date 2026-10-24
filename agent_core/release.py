"""Which code a worker ran: the Git commit of D:\\Agents (2026-10-21).

Until today the source tree had no version history; backups and OPERATIONS.txt helped recovery but could not say
which code, prompts and skills a task actually ran on. Now each worker attempt records the commit it started from,
and a restart refuses to switch the agents to uncommitted changes, so every running release is a commit.
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def commit(root=ROOT):
    """The current commit id, read from .git without running git (cheap enough for every worker start); '' if none."""
    git = Path(root) / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head
        ref = head[5:]
        loose = git / ref
        if loose.exists():
            return loose.read_text(encoding="utf-8").strip()
        for line in (git / "packed-refs").read_text(encoding="utf-8").splitlines():
            if line.endswith(" " + ref):
                return line.split()[0]
    except OSError:
        pass
    return ""


def uncommitted(root=ROOT):
    """Tracked files changed since the last commit (git status --porcelain, untracked excluded); [] when clean."""
    out = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    if out.returncode:
        raise RuntimeError("git status failed: " + out.stderr.strip()[:300])
    return [line[3:] for line in out.stdout.splitlines() if line.strip()]
