"""The installation's settings: a .env file in the home folder (or a config.bat of `set` lines), read once at start.

Every setting is an environment variable (tokens, keys, the owner's Telegram id). The file is `KEY=value` lines;
`#` starts a comment, surrounding quotes are dropped, and a variable already set in the environment wins over the
file, so a shell or a service manager can override it. Secrets never leave the environment: nothing here logs a value.
"""
import os
from pathlib import Path


def settings_file(home) -> Path:
    path = Path(home) / ".env"
    return path if path.exists() else Path(home) / "config.bat"


def settings_keys(home) -> set:
    """The names the home's settings file defines, without their values (for environments that must not carry them)."""
    return set(_pairs(settings_file(home)))


def _pairs(path) -> dict:
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
    return found


def settings_file(home) -> Path:
    path = Path(home) / ".env"
    return path if path.exists() else Path(home) / "config.bat"


def settings_keys(home) -> set:
    """The names the home's settings file defines, without their values (for environments that must not carry them)."""
    return set(_pairs(settings_file(home)))


def _pairs(path) -> dict:
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
    return found


def load_env(home) -> dict:
    """Read <home>/.env (else <home>/config.bat) into os.environ, without overriding what is set; returns what the
    file defined. Lines that are not settings (a batch file's `@echo off`, `rem`) carry no `=` and are skipped."""
    found = _pairs(settings_file(home))
    for key, value in found.items():
        os.environ.setdefault(key, value)
    return found
