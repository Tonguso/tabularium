"""Which code a worker ran: the Git commit of the home folder (2026-10-21).

Until today the source tree had no version history; backups and OPERATIONS.txt helped recovery but could not say
which code, prompts and skills a task actually ran on. Now each worker attempt records the commit it started from,
and a restart refuses to switch the agents to uncommitted changes, so every running release is a commit.
"""
from tabularium.paths import home as _home
import subprocess
from pathlib import Path

ROOT = _home()


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
                         capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if out.returncode:
        raise RuntimeError("git status failed: " + out.stderr.strip()[:300])
    return [line[3:] for line in out.stdout.splitlines() if line.strip()]


def identity(root=ROOT):
    """Fingerprint versioned executable code/resources, not mutable task data or memory."""
    import hashlib
    try:
        result = subprocess.run(['git', '-C', str(root), 'ls-files', '-z'], capture_output=True, timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except OSError:
        result = None
    if result is None or result.returncode:
        return {'commit': '', 'digest': ''}          # not a Git checkout: no release identity, no releases
    digest = hashlib.sha256()
    for name in sorted(result.stdout.decode('utf-8').split('\0')):
        if not name or name.startswith(('docs/', 'tests/', 'public/')):
            continue
        if Path(name).suffix not in ('.py', '.bat', '.json', '.js', '.html', '.css'):
            continue
        digest.update(name.encode()+b'\0')
        digest.update((Path(root)/name).read_bytes())
    return {'commit': commit(root), 'digest': digest.hexdigest()}


def same_source(a, b):
    return bool(a and b and a.get('digest') and a.get('digest') == b.get('digest'))


def test_version(root, target):
    """Both suites certify only source unchanged across the entire test run."""
    import sys
    if not same_source(identity(root), target) or uncommitted(root):
        raise RuntimeError('Source changed before tests; request a new release')
    python = str(Path(sys.executable).with_name('python.exe')) if sys.platform == 'win32' else sys.executable
    for directory in ('tests',):
        result = subprocess.run([python, '-B', '-X', 'utf8', '-m', 'unittest', 'discover', '-s', str(root/directory)],
                                capture_output=True, timeout=300, encoding='utf-8', errors='replace',
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        log = root/'logs/release-tests.log'
        log.parent.mkdir(parents=True, exist_ok=True)
        output = log.open('a', encoding='utf-8')
        try: output.write(result.stdout+'\n'+result.stderr)
        finally: output.close()
        if result.returncode:
            raise RuntimeError(f'Tests failed in {directory}; see logs/release-tests.log')
    if not same_source(identity(root), target) or uncommitted(root):
        raise RuntimeError('Source changed during tests; request a new release')
    return target


# Capture actual disk identity at first import, never a version claimed by the launcher.
LOADED = identity()
