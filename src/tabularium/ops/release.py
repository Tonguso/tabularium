"""Which code a worker ran: the Git commit of the checkout that holds the package (2026-10-21; home-independent 2026-11-09).

Until today the source tree had no version history; backups and OPERATIONS.txt helped recovery but could not say
which code, prompts and skills a task actually ran on. Now each worker attempt records the commit it started from,
and a restart refuses to switch the agents to uncommitted changes, so every running release is a commit.
"""
from tabularium.paths import PACKAGE, child_env, home as _home
import io
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

MARKER = "RELEASE.json"           # written beside the package in a snapshot: the release identity it was exported as
SNAPSHOTS = ".release"            # <home>/.release/<commit>/src/tabularium
PIDS = ".pids"                    # <snapshot>/.pids/<pid>: the processes running this snapshot; it is never pruned under them
KEEP_SNAPSHOTS = 2
# What a release is: the package it ships (archive() packs exactly this) and the suite that certifies it. Agent folders
# are the home's data, read live by the running agents: the dashboard writes routing.json there and agents edit their
# own skills. Counted as source they failed every release that met a dashboard save (three on 2026-11-15) and could stop
# an agent at startup ('Source changed during agent startup').
SHIPPED = "src/tabularium/"
CERTIFIED = (SHIPPED, "tests/")

ROOT = _home()


def _register():
    """A process running from a snapshot leaves its pid in it (2026-11-14: a worker's snapshot was pruned under it by
    the second release since, and its next lazy import failed)."""
    if (PACKAGE / MARKER).exists():
        try:
            folder = PACKAGE.parent.parent / PIDS
            folder.mkdir(exist_ok=True)
            (folder / str(os.getpid())).write_text("", encoding="utf-8")
        except OSError:
            pass


_register()


def in_use(base):
    """True while a live process runs from snapshot `base`; the pids of dead processes are forgotten."""
    from tabularium.ops.supervisor import alive
    busy = False
    for entry in (Path(base) / PIDS).glob("*"):
        try:
            if entry.name.isdigit() and alive(int(entry.name)):
                busy = True
            else:
                entry.unlink(missing_ok=True)
        except OSError:
            busy = True
    return busy


def repo(start=PACKAGE):
    """The Git checkout that holds the package, else None: an installed package has no release of its own, a home
    that is a Git repository of its own data is not the code's version, and a snapshot (RELEASE.json beside the
    package) is the export of a commit, whatever checkout it sits under."""
    if (start / MARKER).exists():
        return None
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
    """Tracked release files (SHIPPED and its suite) changed since the last commit; [] when clean. The home's data in
    the agent folders is not a release file, whatever git tracks."""
    repo = Path(checkout) if checkout else REPO
    if repo is None:
        return []
    out = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if out.returncode:
        raise RuntimeError("git status failed: " + out.stderr.strip()[:300])
    changed = [line[3:].split(" -> ")[-1].strip('"') for line in out.stdout.splitlines() if line.strip()]
    return [name for name in changed if name.startswith(CERTIFIED)]


def identity(checkout=None):
    """Fingerprint of the code: the checkout's versioned executable files and resources, never task data or memory.
    An installed package without a checkout is fingerprinted from its own files, so startup and adoption still agree."""
    import hashlib
    digest = hashlib.sha256()
    repo = Path(checkout) if checkout else REPO
    if repo is None:
        if (PACKAGE / MARKER).exists():                       # a snapshot carries the identity it was exported as
            return json.loads((PACKAGE / MARKER).read_text(encoding="utf-8"))
        for path in sorted(p for p in PACKAGE.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
            if path.suffix in ('.py', '.bat', '.json', '.js', '.html', '.css'):
                digest.update(path.relative_to(PACKAGE).as_posix().encode() + b'\0')
                digest.update(path.read_bytes())
        return {'commit': '', 'digest': digest.hexdigest()}
    result = subprocess.run(['git', '-C', str(repo), 'ls-files', '-z'], capture_output=True, timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError('Cannot identify release source')
    for name in sorted(result.stdout.decode('utf-8').split('\0')):
        if not name.startswith(SHIPPED):                          # the shipped package only, as archive() packs it
            continue
        if Path(name).suffix not in ('.py', '.bat', '.json', '.js', '.html', '.css'):
            continue
        digest.update(name.encode()+b'\0')
        digest.update((repo/name).read_bytes())
    return {'commit': commit(repo), 'digest': digest.hexdigest()}


def archive(commit_id):
    """The committed tree as a tar from git: the working tree's edits are never in it."""
    out = subprocess.run(["git", "-C", str(REPO), "archive", "--format=tar", commit_id, "src/tabularium"],
                         capture_output=True, timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if out.returncode:
        raise RuntimeError("git archive failed: " + out.stderr.decode("utf-8", errors="replace").strip()[:300])
    return out.stdout


def snapshot(target, root=None):
    """The exported package of release `target` (commit and digest), under <home>/.release/<commit>/src: what every
    component and worker runs. Exported once, from the commit, with the identity written beside the package; the
    export of the same release is reused; snapshots beyond KEEP_SNAPSHOTS go. None without a checkout (an installed
    package runs itself). Until 2026-11-12 every process imported the package from the working tree, so an edit on
    disk reached running agents through their lazy imports and every new worker before any test or commit (the
    themes->dockets rename on disk broke the docket loop in all four agents for two hours)."""
    if REPO is None or not target or not target.get("commit"):
        return None
    base = Path(root or ROOT) / SNAPSHOTS / target["commit"]
    marker = base / "src" / "tabularium" / MARKER
    try:
        if json.loads(marker.read_text(encoding="utf-8")) == target:
            return base / "src"
    except (OSError, ValueError):
        pass
    tmp = base.with_name(base.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    with tarfile.open(fileobj=io.BytesIO(archive(target["commit"]))) as tar:
        tar.extractall(tmp, filter="data")
    (tmp / "src" / "tabularium" / MARKER).write_text(json.dumps(target), encoding="utf-8")
    shutil.rmtree(base, ignore_errors=True)
    os.replace(tmp, base)
    kept = sorted((p for p in base.parent.iterdir() if p.is_dir() and not p.name.endswith(".tmp")),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    for old in kept[KEEP_SNAPSHOTS:]:
        if not in_use(old):
            shutil.rmtree(old, ignore_errors=True)
    return base / "src"


def current(root=None):
    """The release a fresh component runs: the tested one while a release is in flight or was the last to complete,
    else the checkout's commit as it stands. A crash restart therefore never runs untested code."""
    path = Path(root or ROOT) / "dashboard" / "restart_status.json"
    try:
        request = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        request = {}
    if request.get("tested") and request.get("status") in ("draining", "complete"):
        return request["tested"]
    return identity()


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
