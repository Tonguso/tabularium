"""Retention: nothing on disk grows forever (2026-10-11).

Run daily by the supervisor (python retention.py sweeps once by hand). Every action is appended to logs/retention.log.
Rules, oldest-first, sizes measured before acting:

- Trials: directories under <agent>/scratch named tools_trial, trials, node_modules, .venv or ending in _venv are
  throwaway installs (a 2.3 GB document-conversion trial sat under cato/scratch on 2026-10-10), and so are
  smoke_* test folders at any depth under scratch. Older than TRIAL_DAYS: deleted. A tool that is meant to stay
  lives under <agent>/tools or D:\\Agents\\tools, never in scratch.
- Finished background tasks: <agent>/task_state/background/<id> older than TASK_DAYS (and not active in jobs.sqlite3)
  is zipped to <agent>/task_state/background/archive/<id>.zip and the folder removed. The job row stays, so the
  dashboard still lists it; a result file that is gone reads as 'archived'.
- Context archives: files under <agent>/context_archive older than ARCHIVE_DAYS are zipped per month into
  <agent>/context_archive/<chat>/<YYYY-MM>.zip and removed.
Nothing here touches memory, skills, reports, logs or code.
"""
from tabularium.paths import home as _home
import shutil, sqlite3, time, zipfile
from contextlib import closing
from pathlib import Path
from tabularium.core.roster import AGENTS

ROOT = _home()
TRIAL_DAYS, TASK_DAYS, ARCHIVE_DAYS = 7, 14, 30
TRIAL_NAMES = ("tools_trial", "trials", "node_modules", ".venv")
LOG = ROOT / "logs" / "retention.log"
ACTIVE = ("queued", "starting", "running", "cancelling", "needs_answer", "waiting_on_delegate")


def _log(line):
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {line}\n")


def _size(path):
    total = 0
    for p in Path(path).rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total



def _newest_mtime(path):
    newest = 0
    for p in Path(path).rglob("*"):
        try:
            if p.is_file():                      # directories are re-stamped by every listing; files tell the age
                newest = max(newest, p.stat().st_mtime)
        except OSError:
            pass
    return newest or Path(path).stat().st_mtime


def sweep_trials(root, now, days=TRIAL_DAYS):
    actions = []
    for agent in AGENTS:
        scratch = Path(root) / agent / "scratch"
        if not scratch.exists():
            continue
        # Smoke-test folders (smoke_<agent>_xxxx, left by test runs whose TEMP pointed into scratch: 41 under
        # cato/scratch/agent_refactor/tmp on 2026-10-11) are trials wherever they sit under scratch.
        candidates = [d for d in scratch.iterdir()] + [d for d in scratch.rglob("smoke_*") if d.parent != scratch]
        for d in candidates:
            if not d.is_dir() or not (d.name in TRIAL_NAMES or d.name.endswith("_venv") or d.name.startswith("smoke_")):
                continue
            age = (now - _newest_mtime(d)) / 86400
            if age > days:
                size = _size(d)
                shutil.rmtree(d, ignore_errors=True)
                actions.append(f"deleted trial {d} ({size // 1_000_000} MB, untouched {age:.0f} days)")
    return actions


def sweep_tasks(root, now, days=TASK_DAYS):
    actions = []
    for agent in AGENTS:
        base = Path(root) / agent / "task_state" / "background"
        jobs = base / "jobs.sqlite3"
        if not jobs.exists():
            continue
        try:
            with closing(sqlite3.connect(jobs, timeout=5)) as db:
                active = {r[0] for r in db.execute(f"SELECT id FROM tasks WHERE status IN ({','.join('?' * len(ACTIVE))})", ACTIVE)}
        except sqlite3.Error:
            continue
        archive = base / "archive"
        for d in base.iterdir():
            if not d.is_dir() or not d.name.isdigit() or int(d.name) in active:
                continue
            if (now - _newest_mtime(d)) / 86400 <= days:
                continue
            archive.mkdir(exist_ok=True)
            target = archive / f"{d.name}.zip"
            size = _size(d)
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
                for f in d.rglob("*"):
                    if f.is_file():
                        try:
                            z.write(f, f.relative_to(d).as_posix())
                        except OSError:
                            pass
            shutil.rmtree(d, ignore_errors=True)
            actions.append(f"archived task {agent}/{d.name} ({size // 1_000_000} MB -> {target.stat().st_size // 1_000_000} MB zip)")
    return actions


def sweep_context_archives(root, now, days=ARCHIVE_DAYS):
    actions = []
    for agent in AGENTS:
        base = Path(root) / agent / "context_archive"
        if not base.exists():
            continue
        for chat in base.iterdir():
            if not chat.is_dir():
                continue
            buckets = {}
            for f in chat.iterdir():
                if f.is_file() and f.suffix != ".zip" and (now - f.stat().st_mtime) / 86400 > days:
                    buckets.setdefault(time.strftime("%Y-%m", time.localtime(f.stat().st_mtime)), []).append(f)
            for month, files in buckets.items():
                target = chat / f"{month}.zip"
                size = sum(f.stat().st_size for f in files)
                with zipfile.ZipFile(target, "a", zipfile.ZIP_DEFLATED) as z:
                    for f in files:
                        z.write(f, f.name)
                for f in files:
                    f.unlink(missing_ok=True)
                actions.append(f"archived {len(files)} context files {agent}/{chat.name}/{month} ({size // 1_000_000} MB)")
    return actions


def sweep(root=ROOT, now=None):
    now = now or time.time()
    actions = sweep_trials(root, now) + sweep_tasks(root, now) + sweep_context_archives(root, now)
    for a in actions:
        _log(a)
    if not actions:
        _log("sweep: nothing to do")
    return actions


if __name__ == "__main__":
    for line in sweep():
        print(line)
