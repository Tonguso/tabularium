"""Publish agent model status for the dashboard, and the operator command for restarts.

    python agent_control.py restart [cato tacitus callimachus vitruvius] [--no-tests] [--uncommitted]
        Refuses if tracked files have uncommitted changes (commit first: every release is a Git commit), runs the
        offline test suites (refuses on failure), then writes <agent>/task_state/drain for each agent.
        The agent finishes its queued work, exits, and the supervisor starts it again on the current code.
    python agent_control.py status
        Supervisor state and each agent's health phase.
    python agent_control.py open
        Start the agents if needed and open the dashboard in its own window (the taskbar icon).
    python agent_control.py start_all
        Start the supervisor if nothing is running; otherwise request a restart of every agent (what start_all.bat does).
    python agent_control.py lessons [agent ...]
        Lesson triage: expire candidates untouched for 30 days, print the digest (proposals first, then themes by repeats).
    python agent_control.py eval <agent> [--collect] [--k N] [--sample N]
        Regression eval (agent_core.regression): rerun the agent's accepted past tasks in isolation and grade them
        against the accepted answers. --collect adds new cases from finished tasks first. The reruns queue on the
        agent's own background worker, so run it when the agent is quiet; a 20-case run takes an hour or more.
"""
import os
import threading
import time
from pathlib import Path
from agent_runtime import atomic_json
from agent_core.roster import AGENTS
from provider_routing import read, config


def start_controls(root, describe):
    root=Path(root)
    def run():
        while True:
            try:
                state=describe()
                config(root)
                state.update(routing_version=1,routing_enabled=True,
                             routing_revision=read(root/'routing.json').get('revision'),
                             pid=os.getpid(),updated=time.time())
                state.pop('options',None)
                atomic_json(root/'task_state/ui_state.json',state)
            except Exception as exc:
                print('[dashboard-status]',type(exc).__name__)
            time.sleep(2)
    threading.Thread(target=run,name='dashboard-status',daemon=True).start()




def request_restart(agents=AGENTS, run_tests=True, root=None, allow_uncommitted=False):
    """Write the drain flags. With run_tests, both offline suites must pass first (a broken runtime would otherwise
    come back broken). The agents restart only onto committed code, so every release they run is a Git commit
    (2026-10-21); allow_uncommitted is for an emergency, and says so. Returns the list of agents flagged."""
    import subprocess, sys
    root = Path(root or Path(__file__).resolve().parent)
    bad = [a for a in agents if a not in AGENTS]
    if bad:
        raise ValueError("Unknown agent: " + ", ".join(bad))
    if (root / ".git").exists() and not allow_uncommitted:
        from agent_core import release
        changed = release.uncommitted(root)
        if changed:
            raise RuntimeError("Uncommitted changes; commit them first so the agents run a known release "
                               "(or pass --uncommitted in an emergency):\n  " + "\n  ".join(changed[:20]))
    if run_tests:
        for directory in (root / "tests", root / "cato" / "tests"):
            result = subprocess.run([sys.executable, "-X", "utf8", "-m", "unittest", "discover", "-s", str(directory)],
                                    capture_output=True, timeout=300, encoding="utf-8", errors="replace")
            if result.returncode:
                raise RuntimeError("Tests failed in " + directory.name + "; no restart requested.\n" + (result.stderr or result.stdout)[-2000:])
    for agent in agents:
        flag = root / agent / "task_state" / "drain"
        flag.parent.mkdir(parents=True, exist_ok=True)
        import uuid
        from supervisor import read_json
        health = read_json(flag.parent/'health.json')
        prior = read_json(flag)
        if not prior.get('id') or prior.get('instance') != health.get('instance'):
            atomic_json(flag, {'id': uuid.uuid4().hex, 'instance': health.get('instance'), 'requested': time.time()})
    return list(agents)


def status(root=None):
    from supervisor import live_status
    view = live_status(root)
    lines = ['supervisor: ' + ('running' if view['running'] else 'unknown (no fresh live evidence)')]
    for name, child in view['agents'].items():
        lines.append(f"{name:12} status={child['status']} phase={child['phase']} pid={child['pid']} starts={child['starts']}")
    return '\n'.join(lines)


def supervisor_alive(root=None):
    from supervisor import live_status
    return live_status(root)['running']


def start_all(run_tests=True):
    """The one entry point (start_all.bat): start the supervisor if it is not running (it then starts the agents and
    the dashboard), otherwise ask every agent to finish its work and restart on the current code."""
    import subprocess
    if supervisor_alive():
        request_restart(run_tests=run_tests, allow_uncommitted=True)     # Alex's button: never blocked by an author's edit
        return "Agents already running: restart requested; each finishes its current work, then comes back on the current code."
    done = subprocess.run(["schtasks", "/Run", "/TN", "AgentsSupervisor"], capture_output=True, encoding="utf-8", errors="replace")
    if done.returncode == 0:
        return "Supervisor started through Task Scheduler; the agents and the dashboard come up within a minute."
    subprocess.Popen(["explorer.exe", str(Path(__file__).resolve().parent / "start_supervisor.bat")])
    return "Task AgentsSupervisor is not installed (run 'Install Supervisor.bat' once); started the supervisor directly instead."


def open_dashboard():
    """What the taskbar icon does: make sure the agents are up, then open the dashboard in its own window
    (Edge or Chrome app mode: no tabs, no address bar), falling back to the default browser."""
    import subprocess, urllib.request
    if not supervisor_alive():
        subprocess.run(["schtasks", "/Run", "/TN", "AgentsSupervisor"], capture_output=True)
    url = "http://127.0.0.1:8220/"
    for _ in range(60):
        try:
            urllib.request.urlopen(url, timeout=2)
            break
        except Exception:
            time.sleep(1)
    try:
        import webview            # pywebview over the WebView2 runtime: a window this process owns, so the taskbar
    except ImportError:           # icon is ours (set below), not a browser's rendering of the favicon (2026-10-10)
        import webbrowser
        webbrowser.open(url)
        return "pywebview is not installed; opened " + url + " in the default browser"
    import ctypes
    APP_ID, TITLE = "Tabularium.Agents", "Tabularium"
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)   # groups with the pinned shortcut
    icon = Path(__file__).resolve().parent / "dashboard" / "assets" / "tabularium.ico"

    def set_icon():
        user32 = ctypes.windll.user32
        for _ in range(50):
            hwnd = user32.FindWindowW(None, TITLE)
            if hwnd:
                break
            time.sleep(0.1)
        if hwnd and icon.exists():
            big = user32.LoadImageW(None, str(icon), 1, 256, 256, 0x10)    # IMAGE_ICON, LR_LOADFROMFILE
            small = user32.LoadImageW(None, str(icon), 1, 32, 32, 0x10)
            user32.SendMessageW(hwnd, 0x80, 1, big)                         # WM_SETICON ICON_BIG
            user32.SendMessageW(hwnd, 0x80, 0, small)                       # WM_SETICON ICON_SMALL

    log = Path(__file__).resolve().parent / "logs" / "dashboard-window.log"
    def note(event):
        try:
            with log.open("a", encoding="utf-8") as handle:
                handle.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {event}\n")
        except OSError:
            pass
    try:
        note("open: creating window")
        window = webview.create_window(TITLE, url, width=1320, height=920, min_size=(900, 600))
        window.events.shown += lambda: note("window shown")
        window.events.loaded += lambda: note("page loaded: " + str(window.get_current_url()))
        window.events.closed += lambda: note("window closed")
        webview.start(set_icon, private_mode=False)
    except Exception as exc:
        note(f"ERROR {type(exc).__name__}: {exc}")
        raise
    return "dashboard window closed"


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if args and args[0] == "open":
        print(open_dashboard())
    elif args and args[0] == "start_all":
        print(start_all(run_tests="--no-tests" not in args))
    elif args and args[0] == "restart":
        names = [a for a in args[1:] if not a.startswith("--")] or list(AGENTS)
        done = request_restart(names, run_tests="--no-tests" not in args, allow_uncommitted="--uncommitted" in args)
        print("Restart requested for " + ", ".join(done) + "; each finishes its current work, then comes back.")
    elif args and args[0] == "status":
        print(status())
    elif args and args[0] == "eval":
        from agent_core import regression
        from agent_core.roster import ROLES
        from background_runtime import TaskStore
        from owner_telegram import OWNER_CHAT_ID
        root = Path(__file__).resolve().parent
        name = next((a for a in args[1:] if not a.startswith("--")), "cato")
        if name not in AGENTS:
            sys.exit("Unknown agent: " + name)
        agent_root = root / name
        store = TaskStore(agent_root, ROLES[name])
        cases = regression.load_cases(agent_root)
        if "--collect" in args:
            import json
            path = regression.cases_path(agent_root)
            candidates = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
            new = regression.collect(agent_root, store, limit=20, existing=candidates)
            regression.save_cases(agent_root, candidates + new)
            print(f"{len(new)} candidate(s) collected; excluded from scored benchmarks until independently reviewed.")
            sys.exit(0)
        if "--signal" in args:                                  # research-quality signal on real cases (2026-10-24)
            from agent_core import research_signal
            n = int(args[args.index("--signal") + 1]) if len(args) > args.index("--signal") + 1 and args[args.index("--signal") + 1].isdigit() else 3
            regression.load_config_env(root)
            record = research_signal.run(agent_root, store, OWNER_CHAT_ID, regression.judge_from_env(), sample=n)
            print(research_signal.table(record)); print("Saved:", record["path"])
            sys.exit(0)
        k = int(args[args.index("--k") + 1]) if "--k" in args else 1
        sample = int(args[args.index("--sample") + 1]) if "--sample" in args else 0
        judge = None
        if any(r["check"]["kind"] == "judgment" for c in cases for r in c["requirements"]):
            regression.load_config_env(root)
            judge = regression.judge_from_env()
        record = regression.run(agent_root, store, OWNER_CHAT_ID, cases, judge, k=k, sample=sample)
        print(regression.table(record))
        print("Earlier runs (pass^k, pass@1, n, k):", regression.previous_runs(agent_root))
        print("Saved:", record["path"])
    elif args and args[0] == "lessons":
        # Weekly triage of the lesson backlog (2026-10-20): expire stale candidates, print the digest.
        from agent_core import learning
        from owner_telegram import OWNER_CHAT_ID
        root = Path(__file__).resolve().parent
        for name in [a for a in args[1:] if not a.startswith("--")] or ["cato"]:
            gone = learning.expire(root / name, OWNER_CHAT_ID)
            print(f"== {name}: {len(gone)} candidate(s) expired")
            print(learning.digest(root / name, OWNER_CHAT_ID))
    elif args and args[0] in ("exam", "review"):
        # Weekly memory checks on demand (2026-10-11): the agent's self-test worker picks up the flag within a minute.
        names = [a for a in args[1:] if not a.startswith("--")] or list(AGENTS)
        for name in names:
            flag = Path(__file__).resolve().parent / name / "task_state" / f"memory_{args[0]}_requested"
            flag.parent.mkdir(parents=True, exist_ok=True); flag.touch()
        print(f"Memory {args[0]} requested for " + ", ".join(names) + "; results in <agent>/task_state/memory_" + args[0] + ".json within a few minutes.")
    else:
        print(__doc__)
