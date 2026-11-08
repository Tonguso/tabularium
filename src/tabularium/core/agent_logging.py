"""Durable logging for the agents.

The launchers spawn each agent in its own console window with no redirection, so when a process
dies its traceback dies with the window. That made the 2026-08-28 shutdown undiagnosable: no
reboot, no sleep, no crash record — and no log either.

This tees stdout/stderr to <home>/logs/<name>.log while STILL printing to the console
(Alex reads the windows), records a banner on start, and installs hooks so an unhandled
exception or a normal exit is written to the log before the process goes away.

Usage — first thing after imports, before any other setup:

    from tabularium.core.agent_logging import start_logging
    start_logging("cato")
"""
from tabularium.paths import logs as _logs
import re
import atexit, datetime, os, sys, threading, time, traceback
from pathlib import Path

LOG_DIR = _logs()
MAX_BYTES = 20 * 1024 * 1024        # roll at 20MB so a chatty week can't fill the disk
HEARTBEAT_SECONDS = 120             # ~700 lines/day; pins time of death to within 2 minutes


# Credentials never reach a log file (2026-10-21: a Telegram connection error printed the request URL, which
# carries the bot token, into cato.log and cato-launch.log). Bot tokens are digits:secret; API keys start sk-.
SECRETS = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}|sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}")


def redact(text: str) -> str:
    return SECRETS.sub("<redacted>", text)


def mask_file(path) -> int:
    """Overwrite any credential already in a log file, in place and at the same length, so a process appending to
    it keeps its offset. Returns how many were masked. A file another process holds for writing raises
    PermissionError: run this before that process opens it (supervisor.start does, for the launch logs)."""
    pattern = re.compile(SECRETS.pattern.encode())
    path = Path(path)
    if not path.exists():
        return 0
    hits = list(pattern.finditer(path.read_bytes()))
    if hits:
        with open(path, "r+b") as handle:
            for m in hits:
                handle.seek(m.start())
                handle.write(b"<redacted>".ljust(m.end() - m.start(), b"x"))
    return len(hits)


def _stamp() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class _Tee:
    """Write to the real stream AND the log file. Never let logging kill or block the agent.

    The file is written by one background thread fed through a bounded queue, so a caller's print() returns at
    once whatever the disk does. On 2026-10-10 08:15 a D: volume hiccup made one write on the open log handle hang
    for good; the lock around that write then froze every thread that printed (heartbeats, and the inbox workers
    that print when the health file write fails), in all six processes, while threads that never print lived on.
    The writer also reopens the file on any error, so a stale handle after a remount is replaced, not kept."""

    QUEUE_LINES = 10000

    def __init__(self, stream, handle, path=None):
        import queue
        self._stream, self._handle, self._path = stream, handle, path
        self._queue = queue.Queue(maxsize=self.QUEUE_LINES)
        self._dropped = 0
        threading.Thread(target=self._drain, name="log-writer", daemon=True).start()

    def write(self, s):
        s = redact(s)
        try:
            if self._stream:
                self._stream.write(s)
                self._stream.flush()
        except Exception:
            pass
        try:
            self._queue.put_nowait(s)
        except Exception:
            self._dropped += 1          # full queue: the disk is stuck; losing log lines beats freezing the agent
        return len(s)

    def _drain(self):
        while True:
            s = self._queue.get()
            try:
                self._handle.write(s)
                self._handle.flush()
            except Exception:
                self._reopen(s)

    def _reopen(self, pending):
        if not self._path:
            return
        try:
            self._handle.close()
        except Exception:
            pass
        try:
            self._handle = open(self._path, "a", encoding="utf-8", errors="replace", buffering=1)
            self._handle.write(f"[{_stamp()}] log file reopened after a write failure ({self._dropped} line(s) dropped)\n")
            self._handle.write(pending)
            self._handle.flush()
        except Exception:
            pass

    def flush(self):
        try:
            if self._stream:
                self._stream.flush()
        except Exception:
            pass

    def flush_file(self):
        """Best effort: let the writer thread catch up, then flush whatever handle is current."""
        try:
            deadline = time.time() + 2
            while not self._queue.empty() and time.time() < deadline:
                time.sleep(0.05)
            self._handle.flush()
        except Exception:
            pass

    def isatty(self):
        try:
            return bool(self._stream) and self._stream.isatty()
        except Exception:
            return False


def start_logging(name: str) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"{name}.log"

    # roll if oversized, keeping one previous generation
    try:
        if path.exists() and path.stat().st_size > MAX_BYTES:
            prev = LOG_DIR / f"{name}.1.log"
            prev.unlink(missing_ok=True)
            path.rename(prev)
    except Exception:
        pass

    handle = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
    tee = _Tee(sys.stdout, handle, path)
    sys.stdout = tee
    sys.stderr = tee                    # one writer thread and one file handle for both streams

    print(f"\n{'=' * 72}")
    print(f"[{_stamp()}] {name} STARTED  pid={os.getpid()}  python={sys.version.split()[0]}")
    print(f"{'=' * 72}")

    def _on_exception(exc_type, exc, tb):
        print(f"\n[{_stamp()}] {name} FATAL — unhandled {exc_type.__name__}")
        traceback.print_exception(exc_type, exc, tb)
        tee.flush_file()

    def _on_thread_exception(args):
        print(f"\n[{_stamp()}] {name} thread '{args.thread.name}' died: {args.exc_type.__name__}")
        traceback.print_exception(args.exc_type, args.exc_value, args.exc_traceback)
        tee.flush_file()

    sys.excepthook = _on_exception
    threading.excepthook = _on_thread_exception

    # Heartbeat. Without this the log goes silent whenever Alex isn't messaging, so a death
    # while he's at work is indistinguishable from an idle afternoon and the last log line only
    # marks the last conversation. With it, the final heartbeat IS the time of death.
    def _beat():
        while True:
            time.sleep(HEARTBEAT_SECONDS)
            try:
                print(f"[{_stamp()}] {name} heartbeat pid={os.getpid()}")
            except Exception:
                return

    threading.Thread(target=_beat, name=f"{name}-heartbeat", daemon=True).start()

    # Distinguish "the console window was closed" from "the process was killed". Closing a
    # console sends CTRL_CLOSE_EVENT; a taskkill /F sends nothing. Only one of those leaves
    # a line behind, which is the discriminator the earlier investigations lacked.
    try:
        import win32api  # noqa: PLC0415  (pywin32 is absent in some envs)

        def _console_handler(ctrl_type):
            names = {0: "CTRL_C", 1: "CTRL_BREAK", 2: "CTRL_CLOSE(window closed)",
                     5: "CTRL_LOGOFF", 6: "CTRL_SHUTDOWN"}
            try:
                print(f"[{_stamp()}] {name} RECEIVED {names.get(ctrl_type, ctrl_type)}")
                tee.flush_file()
            except Exception:
                pass
            return False        # let default handling continue

        win32api.SetConsoleCtrlHandler(_console_handler, True)
    except Exception as e:
        print(f"[{_stamp()}] {name} note: console-close handler unavailable ({e})")

    # Runs on a clean interpreter exit (including sys.exit and Ctrl-C after cleanup).
    # It does NOT run if the process is killed outright — and that absence is itself the
    # diagnosis: a log ending without an EXIT line means the process was terminated, not crashed.
    atexit.register(lambda: (print(f"[{_stamp()}] {name} EXIT (clean interpreter shutdown)"),
                             tee.flush_file()))
    return path
