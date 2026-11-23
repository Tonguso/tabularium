"""Which commands may run without the owner's yes: the danger screen for deletion and process kills, the
auto-mode switch, and the deadline and tree kill every command runs under.
"""
import json
import re
import subprocess
import threading
from pathlib import Path
from tabularium.core.outcome import Refused


class CommandGuard:
    """Part of AgentRuntime (tabularium.core.engine): methods and constants moved here unchanged on 2026-11-23."""

    _CATASTROPHIC_CMD = re.compile(
        r'(?i)('
        r'\bformat-volume\b|\bdiskpart\b'
        r'|\bstop-computer\b|\brestart-computer\b|\bshutdown\b'
        r'|\buninstall-|\breg\s+delete\b'
        r')'
    )

    _DELETION_CMD = re.compile(
        r'(?i)(\bremove-item\b|\bdel\b|\berase\b|\brmdir\b|\brd\b|\brm\b)'
    )

    _MASS_DELETE = re.compile(r'(?i)(-recurse\b|/s\b|\*)')

    _DISPOSABLE_DIR = re.compile(r'(?i)\\(?:scratch|temp|tmp|__pycache__|\.pytest_cache|\.ruff_cache|\.mypy_cache)(?:\\|$)'
                                 r'|^(?:\$env:temp|%temp%)')

    _PATH_LITERAL = re.compile(r'''(?i)(?:[a-z]:\\|\$env:temp|%temp%)[^'"\s;|,)}]*''')

    _VAR_ASSIGN = re.compile(r'''\$(\w+)\s*=\s*['"]([^'"]+)['"]''')

    _VAR_REF = re.compile(r'''\$(\w+)([^\s'";|,)}]*)''')

    _NEUTRAL_VARS = {"_", "true", "false", "null", "env", "psitem"}

    _CD = re.compile(r'''(?i)^\s*(?:cd|chdir|set-location|sl|push-location|pushd)\s+(?:-(?:literal)?path\s+)?['"]?([^'";|]+?)['"]?\s*$''')

    _ARG_TOKEN = re.compile(r'''"[^"]*"|'[^']*'|[^\s,]+''')

    _TARGET_FLAGS = {"-path", "-literalpath"}

    _VALUE_FLAGS = {"-filter", "-include", "-exclude", "-erroraction", "-ea"}

    _PROCESS_KILL = re.compile(
        r'(?i)(\bstop-process\b|\bspps\b|\btaskkill\b|\bpkill\b|\|\s*kill\b|\bkill\s+(?=-|\d|\$))'
    )

    _PID_ONLY_KILL = re.compile(
        r'(?i)^\s*(?:stop-process|spps|kill)\s+(?:-id\s+)?\d+(?:\s*,\s*\d+)*'
        r'(?:\s+-(?:force|f|confirm:\$false|erroraction\s+\w+|ea\s+\w+))*\s*$'
        r'|^\s*taskkill(?:\s+/[ft])*\s+/pid\s+\d+(?:\s+/[ft])*\s*$'
    )

    KILL_REFUSAL = Refused("Refused, not executed: process kills must name literal PIDs (Stop-Process -Id N, "
                           "taskkill /PID N). A by-name/filter kill stopped all four agents on 2026-10-06.")

    COMMAND_TIMEOUT = 60

    def _auto_mode_path(self, chat_id: int) -> Path:
        return self.AGENT_DIR / f"auto_{chat_id}.json"

    def get_auto_mode(self, chat_id: int) -> bool:
        if chat_id not in self.auto_modes:
            on = False
            p = self._auto_mode_path(chat_id)
            if p.exists():
                try:
                    on = bool(json.loads(p.read_text(encoding="utf-8")).get("auto", False))
                except Exception:
                    on = False
            self.auto_modes[chat_id] = on
        return self.auto_modes[chat_id]

    def set_auto_mode(self, chat_id: int, on: bool) -> None:
        self.auto_modes[chat_id] = on
        try:
            self._auto_mode_path(chat_id).write_text(json.dumps({"auto": on}), encoding="utf-8")
        except OSError as e:
            print(f"[state] auto mode not saved for chat {chat_id}: {e}")

    def _disposable(self, path: str) -> bool:
        p = path.replace('/', '\\').rstrip('\\')
        return '..' not in p and bool(self._DISPOSABLE_DIR.search(p))

    def _resolve_dir(self, token: str, variables: dict):
        """Absolute directory for a cd argument, or None if it cannot be known."""
        token = token.strip()
        m = re.fullmatch(r'\$(\w+)(.*)', token)
        if m:
            base = variables.get(m.group(1).lower())
            return base + m.group(2) if base else None
        return token if re.match(r'(?i)[a-z]:\\|\$env:temp|%temp%', token) else None

    def _relative_targets(self, statement: str) -> list:
        """Bare (relative) path arguments given to the delete verb itself or, when the delete
        takes its input from a pipe ("Get-ChildItem x | Remove-Item"), to the command feeding it."""
        verb = self._DELETION_CMD.search(statement)
        own = self._bare_args(re.split(r'[|})]', statement[verb.end():], maxsplit=1)[0])
        if own or '|' not in statement[:verb.start()]:
            return own
        source = statement[:verb.start()].split('|')[0].strip().lstrip('{(')
        return self._bare_args(source.split(None, 1)[1] if len(source.split(None, 1)) > 1 else '')

    def _bare_args(self, rest: str) -> list:
        out, expect = [], None                   # expect: "target" or "value" for the next token
        for tok in self._ARG_TOKEN.findall(rest):
            tok = tok.strip('\'"')
            if expect == "value":                # e.g. SilentlyContinue after -ErrorAction
                expect = None
                continue
            if expect == "target":
                expect = None
                if tok and not tok.startswith(('$', '-')) and not self._PATH_LITERAL.match(tok):
                    out.append(tok)
                continue
            low = tok.lower()
            if low in self._TARGET_FLAGS:
                expect = "target"
            elif low in self._VALUE_FLAGS:
                expect = "value"
            elif tok and not tok.startswith(('-', '$', '/')) and not self._PATH_LITERAL.match(tok):
                out.append(tok)
        return out

    def _disposable_statement(self, statement: str, variables: dict, cwd=None) -> bool:
        targets = [m.group(0) for m in self._PATH_LITERAL.finditer(statement)]
        for name, suffix in self._VAR_REF.findall(statement):
            name = name.lower()
            if name in variables:
                targets.append(variables[name] + (suffix if suffix.startswith(('\\', '/')) else ''))
            elif name not in self._NEUTRAL_VARS:
                return False                     # deletes something we cannot see: ask
        cwd_ok = cwd is not None and self._disposable(cwd)
        relative = self._relative_targets(statement)
        # Relative names resolve against the folder the command cd'd into: "cd D:\Agents\coordinator;
        # Remove-Item scratch\x" is inside scratch even though the cd folder itself is not.
        if relative and not (cwd is not None and
                             all(self._disposable(cwd.rstrip('\\/') + '\\' + r) for r in relative)):
            return False                         # relative target outside a known scratch dir: ask
        if not targets and not relative:
            return cwd_ok                        # e.g. "Get-ChildItem | Remove-Item" in the cwd
        return all(self._disposable(t) for t in targets)

    def _kills_by_name(self, command: str) -> bool:
        for statement in re.split(r'[;\n]', command or ""):
            if self._PROCESS_KILL.search(statement) and not self._PID_ONLY_KILL.match(statement):
                return True
        return False

    def _is_dangerous(self, command: str) -> bool:
        c = command or ""
        if self._CATASTROPHIC_CMD.search(c):
            return True
        if not self._DELETION_CMD.search(c):
            return False
        variables = {k.lower(): v for k, v in self._VAR_ASSIGN.findall(c)}
        cwd = None
        # Only mass deletions (recursive or wildcard) can prompt. A single-file delete, and any
        # file edit/write, run without asking -- that's routine work.
        for statement in re.split(r'[;\n]', c):
            cd = self._CD.match(statement)
            if cd:
                cwd = self._resolve_dir(cd.group(1), variables)
                continue
            if self._DELETION_CMD.search(statement) and self._MASS_DELETE.search(statement):
                if not self._disposable_statement(statement, variables, cwd):
                    return True
        return False

    def _kill_tree(self, pid: int) -> None:
        """taskkill /T takes the children and grandchildren too; a plain kill leaves them alive
        and still holding our pipes."""
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            pass

    def _with_deadline(self, fn, seconds: int, on_timeout=None, label: str = "tool"):
        """Run fn() on a worker thread and give up after `seconds`. The tool functions do their
        own CoInitialize, so COM is happy on the worker. The abandoned thread is a daemon: it
        dies with the process, or when on_timeout() kills whatever it was blocked on."""
        box: dict = {}

        def run():
            try:
                box["result"] = fn()
            except Exception as e:           # surface, don't swallow
                box["result"] = f"Error in {label}: {e}"

        t = threading.Thread(target=run, daemon=True, name=f"deadline-{label}")
        t.start()
        t.join(seconds)
        if t.is_alive():
            note = on_timeout() if on_timeout else ""
            print(f"[deadline] {label} exceeded {seconds}s; {note}")
            return (f"{label} did not respond within {seconds} seconds."
                    f"{' (' + note + ')' if note else ''} The worker may still finish; the outcome is UNKNOWN. "
                    f"Do not resend or repeat a change. Inspect saved email receipts, Outbox/Sent Items or the actual file first. "
                    f"Receipts: {self.AGENT_DIR / 'task_state' / 'email_receipts'}")
        return box.get("result", f"Error in {label}: no result")
