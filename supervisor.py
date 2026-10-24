"""The one process that owns the agents and the dashboard.

Task Scheduler starts it at logon (task AgentsSupervisor); it starts the four agents and the dashboard as
children and starts any child again when it exits. Nothing here kills a healthy process:

- A restart is a flag. `python agent_control.py restart [agent ...]` (or the dashboard's Restart button) writes
  <agent>/task_state/drain. The agent stops taking Telegram updates, finishes queued work, acknowledges that request and exits RESTART_EXIT,
  and comes back here within a few seconds on the current code (engine.serve_forever).
- The only kill is of a child that is stalled past its own health deadline by five minutes.
- A child that crashes three times within ten minutes waits five minutes before the next start, and Alex is told.

State for the dashboard and for `agent_control.py status`: logs/supervisor_state.json.
Maintenance: an existing logs/supervisor.pause file stops all starting and killing until it is removed.
"""
import json, os, socket, subprocess, sys, time, uuid
from pathlib import Path
from agent_core.roster import AGENTS

ROOT = Path(__file__).resolve().parent
PY = r"C:\ProgramData\miniconda3\envs\ai_agents\python.exe"
STATE = ROOT / "logs" / "supervisor_state.json"
PAUSE = ROOT / "logs" / "supervisor.pause"
INSTANCE_PORT = 8210
POLL_SECONDS = 3
SWEEP_SECONDS = 24 * 3600     # retention sweep once a day (first one shortly after start)
CRASH_WINDOW, CRASH_COUNT, CRASH_BACKOFF = 600, 3, 300
STALL_GRACE = 300
RESTART_EXIT = 75
STATUS_STALE = 150
HEALTH_STALE = 600     # an idle agent rewrites health.json every second; ten silent minutes means a hung worker


def command(name):
    if name == "dashboard":
        return [PY, str(ROOT / "dashboard.py")]
    return ["cmd.exe", "/d", "/c", str(ROOT / "run_agent.bat"), name]


def process_created(pid):
    import pywintypes, win32api, win32process
    handle = None
    try:
        handle = win32api.OpenProcess(0x1000, False, int(pid))
        if win32process.GetExitCodeProcess(handle) != 259:
            return 0
        return win32process.GetProcessTimes(handle)['CreationTime'].timestamp()
    except pywintypes.error as exc:
        return 0 if exc.winerror == 87 else None  # No such PID versus unable to observe it.
    except (TypeError, ValueError):
        return 0
    finally:
        if handle is not None:
            handle.Close()


def alive(pid):
    return bool(process_created(pid))


class Child:
    def __init__(self, name):
        self.name, self.proc, self.adopted, self.started, self.exits, self.starts = name, None, None, 0.0, [], 0
        self.python_pid = None
        self.instance, self.created, self.handle, self.last_exit = None, None, None, None

    def running(self):
        if self.proc is not None:
            return self.proc.poll() is None
        if self.handle is not None:
            import win32process
            return win32process.GetExitCodeProcess(self.handle) == 259
        return self.adopted is not None and alive(self.adopted)

    def tracked(self):
        return self.proc is not None or self.adopted is not None

    def pid(self):
        if not self.running():
            return None
        return self.proc.pid if self.proc is not None else self.adopted

    def mark_exit(self):
        code = self.proc.returncode if self.proc is not None else "unknown (adopted)"
        if self.handle is not None:
            import win32process
            code = win32process.GetExitCodeProcess(self.handle)
            self.handle.Close()
            self.handle = None
        self.proc, self.adopted, self.python_pid = None, None, None
        return code


def adopt_existing(children):
    """A supervisor that starts while agents already run (a supervisor restart, or the first run after the old
    launchers) takes them over instead of starting duplicates that would lose the single-instance port."""
    import subprocess as sp
    query = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'D:\\\\Agents' } | "
             "Select-Object ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json -Compress")
    try:
        out = sp.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", query], capture_output=True,
                     timeout=60, encoding="utf-8", errors="replace", creationflags=getattr(sp, "CREATE_NO_WINDOW", 0)).stdout
        rows = json.loads(out or "[]")
    except (sp.SubprocessError, ValueError, OSError):
        return []
    rows = [rows] if isinstance(rows, dict) else rows
    adopted = []
    previous = read_json(STATE).get("children", {})
    for child in children:
        prior = previous.get(child.name, {})
        child.exits = prior.get('crashes', [])
        child.last_exit = prior.get('last_exit')
        child.starts = prior.get('starts', 0)
        for r in sorted(rows, key=lambda row: row['ProcessId'] != prior.get('pid')):
            cmd = (r.get("CommandLine") or "").lower()
            name = (r.get("Name") or "").lower()
            if child.name == "dashboard":
                hit = name.startswith("python") and "dashboard.py" in cmd
            else:
                hit = (name == "cmd.exe" and 'run_agent.bat" ' + child.name in cmd) or \
                      (name.startswith("python") and child.name + "\\telegram.py" in cmd)
            if hit and alive(r["ProcessId"]):
                import win32api
                child.adopted = int(r["ProcessId"])
                child.handle = win32api.OpenProcess(0x1000, False, child.adopted)
                child.created = process_created(child.adopted)
                prior = previous.get(child.name, {})
                if prior.get('pid') == child.adopted and prior.get('created') == child.created:
                    child.instance = prior.get('instance')
                    child.python_pid = prior.get('python_pid')
                    child.started = prior.get('started', 0)
                if not child.instance and child.name in AGENTS:
                    # Recover identity after losing the snapshot only from an actual descendant.
                    health = read_json(ROOT/child.name/'task_state/health.json')
                    parents = {row['ProcessId']: row.get('ParentProcessId') for row in rows}
                    pid, seen = health.get('pid'), set()
                    born = process_created(pid)
                    while pid and pid not in seen and pid != child.adopted:
                        seen.add(pid)
                        pid = parents.get(pid)
                    if pid == child.adopted and born and health.get('last_progress', 0) >= born:
                        child.instance = health.get('instance')
                        child.python_pid = health.get('pid')
                        child.started = child.created
                adopted.append((child.name, child.adopted))
                break
    return adopted


def observe_exit(child, now, health, request):
    instance, pid = child.instance, child.python_pid
    code = child.mark_exit()
    planned = bool(code == RESTART_EXIT and instance and request.get('id')
                   and request.get('instance') == instance and health.get('instance') == instance
                   and matching_health(instance, pid, child.started, health) and health.get('restart') == request)
    child.last_exit = {'time': now, 'code': code, 'planned': planned}
    child.exits = [t for t in child.exits if now-t < CRASH_WINDOW]
    if not planned:
        child.exits.append(now)
    return code


def matching_health(instance, pid, started, health):
    return bool(instance and health.get('instance') == instance and health.get('pid')
                and (pid is None or health['pid'] == pid) and health.get('last_progress', 0) >= started)


def live_status(root=None, now=None):
    """One evidence-based view for the CLI, dashboard and agent operations tool."""
    root, now = Path(root or ROOT), time.time() if now is None else now
    state = read_json(root/'logs/supervisor_state.json')
    fresh = bool(0 <= now-state.get('updated', 0) < 30 and state.get('created')
                 and process_created(state.get('pid')) == state['created'])
    result = {}
    for name in AGENTS + ('dashboard',):
        record = state.get('children', {}).get(name, {})
        health = read_json(root/name/'task_state/health.json') if name in AGENTS else {}
        observed = process_created(record.get('pid')) if fresh and record.get('running') else 0
        running = bool(fresh and record.get('running') and record.get('created')
                       and observed == record['created'])
        matched = running and matching_health(record.get('instance'), record.get('python_pid'),
                                              record.get('started', 0), health)
        healthy = bool(matched and 0 <= now-health.get('last_progress', 0) < STATUS_STALE
                       and alive(health['pid']))
        request = read_json(root/name/'task_state/drain')
        draining = bool(running and request.get('id') and request.get('instance') == record.get('instance'))
        transport = read_json(root/name/'task_state/transport.json') if healthy else {}
        polling = bool(healthy and transport.get('pid') == health['pid'] and transport.get('poll_ok')
                       and 0 <= now-transport.get('time', 0) < 90)
        phase = health.get('phase', 'unknown') if healthy else 'unknown'
        lifecycle = 'unknown' if not fresh or observed is None else ('stopped' if not running else 'starting')
        if running and name == 'dashboard':
            lifecycle = 'running'
        elif draining:
            lifecycle = 'draining'
        elif healthy:
            lifecycle = 'ready' if polling and phase == 'idle' else ('working' if polling else 'connection issue')
        elif running and matched:
            lifecycle = 'unresponsive'
        if fresh and not running and record.get('backoff_until', 0) > now:
            lifecycle = 'backoff'
        result[name] = dict(status=lifecycle, phase=phase, pid=health.get('pid') if healthy else None,
                            running=running, health_fresh=healthy, polling_fresh=polling,
                            last_progress=health.get('last_progress') if matched else None,
                            restart_pending=draining, starts=record.get('starts'))
    return {'running': fresh, 'updated': state.get('updated'), 'agents': result}


def decide(child, now, health=None, paused=False):
    """'start', 'wait', 'kill' or None for one child, from its own history and the agent's health file.
    Pure, so it is testable: health is the parsed health.json (None for the dashboard)."""
    if paused:
        return None
    if child.running():
        if health and matching_health(child.instance, agent_pid(child), child.started, health):
            if health.get("phase") not in (None, "idle") and health.get("deadline") and now > health["deadline"] + STALL_GRACE:
                return "kill"
            if health.get("last_progress") and now - health["last_progress"] > HEALTH_STALE:
                return "kill"            # alive but silent: the 2026-10-10 hang (threads frozen on a dead log handle)
        return None
    recent = [t for t in child.exits if now - t < CRASH_WINDOW]
    if len(recent) >= CRASH_COUNT and now - recent[-1] < CRASH_BACKOFF:
        return "wait"
    return "start"


def agent_pid(child):
    """The python process behind a cmd child, as the agent writes it into health.json; None until known."""
    return getattr(child, "python_pid", None)


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def start(child):
    log = ROOT / "logs" / f"{child.name}-launch.log"
    log.parent.mkdir(exist_ok=True)
    try:                                   # nothing holds the launch log between runs: mask any credential in it
        from agent_logging import mask_file
        mask_file(log)
    except (OSError, ImportError) as exc:
        print(f"[supervisor] could not check {log.name} for credentials: {type(exc).__name__}")
    with log.open("ab") as out:
        out.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] supervisor starting {child.name}\n".encode())
    # run_agent.bat appends the agent's own output to this log itself; holding the file open here made cmd's
    # redirection fail with a sharing violation. The dashboard has no launcher, so its output goes to the log directly.
    if child.name == "dashboard":
        out = log.open("ab")
    else:
        out = subprocess.DEVNULL
    child.instance = uuid.uuid4().hex
    child.python_pid = None
    (ROOT/child.name/'task_state/drain').unlink(missing_ok=True)
    env = dict(os.environ, AGENT_LAUNCH_ID=child.instance)
    try:
        child.proc = subprocess.Popen(command(child.name), cwd=str(ROOT), stdout=out, stderr=subprocess.STDOUT,
                                      stdin=subprocess.DEVNULL, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    finally:
        if out is not subprocess.DEVNULL:
            out.close()
    child.started = time.time()
    child.created = process_created(child.proc.pid)
    child.starts += 1
    print(f"[supervisor] started {child.name} pid={child.proc.pid}")


def kill_tree(pid):
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def notify_owner(text):
    """One Telegram line to Alex through Cato's bot; never logs, credentials or task contents."""
    import re, urllib.request
    try:
        config = (ROOT / "config.bat").read_text(encoding="utf-8-sig")
        match = re.search(r'^\s*set\s+"?TELEGRAM_BOT_TOKEN=([^\r\n"]+)', config, re.I | re.M)
        if not match:
            return
        from owner_telegram import OWNER_CHAT_ID
        data = json.dumps({"chat_id": OWNER_CHAT_ID, "text": "Supervisor: " + text}).encode("utf-8")
        request = urllib.request.Request("https://api.telegram.org/bot" + match.group(1).strip() + "/sendMessage",
                                         data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=15):
            pass
    except Exception as exc:
        print(f"[supervisor] owner notification failed: {type(exc).__name__}")


def write_state(children, paused):
    state = {"updated": time.time(), "paused": paused, "pid": os.getpid(), "created": process_created(os.getpid()), "children": {
        c.name: {"pid": c.pid(), "python_pid": agent_pid(c), "started": c.started, "starts": c.starts,
                 "last_exit": c.last_exit, "running": c.running(), "created": c.created,
                 "instance": c.instance, "crashes": c.exits,
                 "backoff_until": c.exits[-1]+CRASH_BACKOFF if decide(c, time.time()) == 'wait' else 0} for c in children}}
    try:
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        os.replace(tmp, STATE)
    except OSError:
        pass


def main():
    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", INSTANCE_PORT))
    except OSError:
        print("[supervisor] already running")
        return
    sys.path.insert(0, str(ROOT))
    from agent_logging import start_logging
    start_logging("supervisor")
    children = [Child(name) for name in AGENTS + ("dashboard",)]
    last_sweep = time.time() - SWEEP_SECONDS + 300
    # What Alex has already been told, kept on disk: a supervisor restart used to repeat every stale self-test and
    # memory-exam failure (10:39 on 2026-10-24, three exams from four and five days earlier).
    told_path = ROOT / "logs" / "supervisor_told.json"
    selftest_told = read_json(told_path) or {}
    def told(key, value):
        selftest_told[key] = value
        try:
            told_path.write_text(json.dumps(selftest_told), encoding="utf-8")
        except OSError as exc:
            print(f"[supervisor] could not save what was told: {type(exc).__name__}")
    for name, pid in adopt_existing(children):
        print(f"[supervisor] adopted running {name} pid={pid}")
    notified = set()
    while True:
        now = time.time()
        paused = PAUSE.exists()
        for child in children:
            health = read_json(ROOT / child.name / "task_state" / "health.json") if child.name in AGENTS else {}
            if child.tracked() and not child.running():
                request = read_json(ROOT/child.name/'task_state/drain')
                code = observe_exit(child, now, health, request)
                print(f"[supervisor] {child.name} exited with code {code}; planned={child.last_exit['planned']}")
            if child.running() and matching_health(child.instance, child.python_pid, child.started, health):
                child.python_pid = health.get("pid")       # the health file written by this start, not a stale one
            action = decide(child, now, health, paused)
            if action == "start":
                start(child)
            elif action == "wait" and child.name not in notified:
                notified.add(child.name)
                print(f"[supervisor] {child.name} is crash-looping; waiting {CRASH_BACKOFF}s")
                notify_owner(f"{child.name} exited {CRASH_COUNT} times in {CRASH_WINDOW // 60} minutes; "
                             f"waiting {CRASH_BACKOFF // 60} minutes before the next start. See logs\\{child.name}-launch.log.")
            elif action == "kill":
                print(f"[supervisor] {child.name} stalled past its deadline; stopping it")
                notify_owner(f"{child.name} stalled at {health.get('phase')} on task {health.get('task')} past its own "
                             "deadline; stopping it so it can restart. Saved actions are kept.")
                kill_tree(child.pid())
            if action == "start" and child.name in notified and len([t for t in child.exits if now - t < CRASH_WINDOW]) < CRASH_COUNT:
                notified.discard(child.name)
        if now - last_sweep > SWEEP_SECONDS and not paused:
            last_sweep = now
            try:
                import retention
                for line in retention.sweep(ROOT, now):
                    print("[supervisor] retention: " + line)
            except Exception as exc:
                print(f"[supervisor] retention failed: {type(exc).__name__}: {exc}")
        for name in AGENTS:                                   # a failed self-test is a broken turn path: say so once
            st = read_json(ROOT / name / "task_state" / "selftest.json")
            if st and not st.get("ok") and selftest_told.get(name) != st.get("time"):
                told(name, st.get("time"))
                notify_owner(f"{name}'s daily self-test failed: {str(st.get('error') or st.get('reply'))[:200]}. "
                             "The process is up but a real turn does not complete; inspect its log.")
            # The weekly memory exam is shown on the dashboard with its questions; it is not sent to Telegram
            # (Alex, 2026-10-24: "is it really necessary that he sends me these supervisor messages?").
        write_state(children, paused)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
