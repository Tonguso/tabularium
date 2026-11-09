"""The installation's settings: a .env file in the home folder (or a config.bat of `set` lines), read once at start.

Every setting is an environment variable (tokens, keys, the owner's Telegram id). The file is `KEY=value` lines;
`#` starts a comment, surrounding quotes are dropped, and a variable already set in the environment wins over the
file, so a shell or a service manager can override it. Secrets never leave the environment: nothing here logs a value.
"""
import os
from pathlib import Path


def load_env(home) -> dict:
    """Read <home>/.env (else <home>/config.bat) into os.environ, without overriding what is set; returns what the
    file defined. Lines that are not settings (a batch file's `@echo off`, `rem`) carry no `=` and are skipped."""
    path = Path(home) / ".env"
    if not path.exists():
        path = Path(home) / "config.bat"
    found = {}
    if not path.exists():
        return found
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key.lower().startswith("set "):          # a batch file's `set NAME=value` or `set "NAME=value"`
            key = key[4:].strip()
            if key.startswith('"') and value.endswith('"'):
                key, value = key[1:], value[:-1]
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            found[key] = value
            os.environ.setdefault(key, value)
    return found
