"""Which code a worker ran: the Git commit of the checkout that holds the package (2026-10-21; home-independent 2026-11-09).

Until today the source tree had no version history; backups and OPERATIONS.txt helped recovery but could not say
which code, prompts and skills a task actually ran on. Now each worker attempt records the commit it started from,
and a restart refuses to switch the agents to uncommitted changes, so every running release is a commit.
"""
from tabularium.paths import PACKAGE, child_env, home as _home
import os
import subprocess
from pathlib import Path

ROOT = _home()


def repo(start=PACKAGE):
    """The Git checkout that holds the package, else None: an installed package has no release of its own, and a home
    that is a Git repository of its own data is not the code's version."""
    for folder in (start, *start.parents):
        if (folder / ".git").exists():
            return folder
    return None


REPO = repo()


def commit(checkout=None):
    """The current commit id of the checkout (default: the package's own), read from .git without running git (cheap
    enough for every worker start); '' if none."""
    repo = Path(checkout) if checkout else REPO
    if repo is None:
        return ""
    git = repo / ".git"
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


def uncommitted(checkout=None):
    """Tracked files changed since the last commit (git status --porcelain, untracked excluded); [] when clean."""
    repo = Path(checkout) if checkout else REPO
    if repo is None:
        return []
    out = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if out.returncode:
        raise RuntimeError("git status failed: " + out.stderr.strip()[:300])
    return [line[3:] for line in out.stdout.splitlines() if line.strip()]


def identity(checkout=None):
    """Fingerprint of the code: the checkout's versioned executable files and resources, never task data or memory.
    An installed package without a checkout is fingerprinted from its own files, so startup and adoption still agree."""
    import hashlib
    digest = hashlib.sha256()
    repo = Path(checkout) if checkout else REPO
    if repo is None:
        for path in sorted(p for p in PACKAGE.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
            if path.suffix in ('.py', '.bat', '.json', '.js', '.html', '.css'):
                digest.update(path.relative_to(PACKAGE).as_posix().encode() + b'\0')
                digest.update(path.read_bytes())
        return {'commit': '', 'digest': digest.hexdigest()}
    result = subprocess.run(['git', '-C', str(repo), 'ls-files', '-z'], capture_output=True, timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError('Cannot identify release source')
    for name in sorted(result.stdout.decode('utf-8').split('\0')):
        if not name or name.startswith(('docs/', 'tests/', 'public/')):
            continue
        if Path(name).suffix not in ('.py', '.bat', '.json', '.js', '.html', '.css'):
            continue
        digest.update(name.encode()+b'\0')
        digest.update((repo/name).read_bytes())
    return {'commit': commit(repo), 'digest': digest.hexdigest()}


def same_source(a, b):
    return bool(a and b and a.get('digest') and a.get('digest') == b.get('digest'))


def test_version(root, target):
    """The checkout's suites certify the source, unchanged across the entire run. They run with the checkout as their
    home and without this home's settings, so a green gate does not depend on this machine's credentials. An installed
    package has no suites to run (it was tested when it was built)."""
    import sys
    from tabularium import config
    if not same_source(identity(), target) or uncommitted():
        raise RuntimeError('Source changed before tests; request a new release')
    if REPO is None:
        return target
    python = str(Path(sys.executable).with_name('python.exe')) if sys.platform == 'win32' else sys.executable
    settings = config.settings_keys(root)
    env = {k: v for k, v in child_env(REPO).items() if k not in settings}
    for directory in ('tests',):
        result = subprocess.run([python, '-B', '-X', 'utf8', '-m', 'unittest', 'discover', '-s', str(REPO/directory)],
                                capture_output=True, timeout=300, encoding='utf-8', errors='replace', env=env, cwd=str(REPO),
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        log = root/'logs/release-tests.log'
        log.parent.mkdir(parents=True, exist_ok=True)
        output = log.open('a', encoding='utf-8')
        try: output.write(result.stdout+'\n'+result.stderr)
        finally: output.close()
        if result.returncode:
            raise RuntimeError(f'Tests failed in {directory}; see logs/release-tests.log')
    if not same_source(identity(), target) or uncommitted():
        raise RuntimeError('Source changed during tests; request a new release')
    return target


# Capture actual disk identity at first import, never a version claimed by the launcher.
LOADED = identity()
