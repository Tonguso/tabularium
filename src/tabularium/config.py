"""The installation's settings: a .env file in the home folder, read once at start.

Every setting is an environment variable (tokens, keys, the owner's Telegram id). The file is `KEY=value` lines;
`#` starts a comment, surrounding quotes are dropped, and a variable already set in the environment wins over the
file, so a shell or a service manager can override it. Secrets never leave the environment: nothing here logs a value.
"""
import os
from pathlib import Path


def load_env(home) -> dict:
    """Read <home>/.env into os.environ (without overriding what is set) and return what the file defined."""
    path = Path(home) / ".env"
    found = {}
    if not path.exists():
        return found
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key.lower().startswith("set "):          # a line pasted from the old config.bat
            key = key[4:].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            found[key] = value
            os.environ.setdefault(key, value)
    return found
