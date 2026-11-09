"""Move the flat layout into the `tabularium` package and rewrite every import (Stage 1 of the restructure, 2026-11-08).

Every module keeps its file name; only its place changes. Three modules are renamed because their package already
carries the name (dashboard.py -> dashboard/server.py, dashboard_audit.py -> dashboard/audit.py,
dashboard_history.py -> dashboard/history.py) and the four agent entry points become role modules
(coordinator/telegram.py -> agents/coordinator.py, ...). The same table drives the later migration of a private checkout:

    python tools/restructure.py            # in the repository root; refuses to run twice

The script does the mechanical part (git mv, import rewriting, test path hacks). The hand-written parts of the
restructure (paths.home(), the CLI, agents.build(), the worker loader) are separate commits on top.
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = Path("src/tabularium")

GROUPS = {
    "core": ["engine", "agent_app", "agent_runtime", "agent_logging", "roster", "reply_envelope", "outcome", "wire_budget",
             "turn_loop", "context_window", "shared_handlers", "session_lifecycle", "responses_context", "taint",
             "command_output", "trace"],
    "records": ["background_runtime", "lifecycle", "task_decisions", "verification", "action_evidence", "outbox",
                "project_store", "worker_lease", "task_cost", "skill_record"],
    "channels": ["owner_telegram", "messaging"],
    "models": ["provider_routing", "subscription_runtime", "subscription_connections", "subscription_usage",
               "openai_chatgpt", "usage"],
    "tools": ["search_routing", "web_content", "sec_edgar", "desktop", "spreadsheet_upload", "video_intake", "knowledge",
              "textdiff"],
    "memory": ["memory_store", "memory_context", "memory_exam", "memory_review", "memory_consolidation",
               "conversation_recall", "archive_search", "image_memory"],
    "learning": ["learning", "lesson_cases", "trials", "corrections", "regression", "research_signal", "interaction_eval",
                 "specialist_review"],
    "delegation": ["delegation_contract", "research_client", "work_client", "delegate_local"],
    "ops": ["supervisor", "release", "retention", "operations", "scorecard", "agent_control"],
}
GROUP_OF = {m: g for g, ms in GROUPS.items() for m in ms}
# old module name -> (group, new module name) for the renamed ones
RENAMED = {"dashboard": ("dashboard", "server"), "dashboard_audit": ("dashboard", "audit"),
           "dashboard_history": ("dashboard", "history")}
# where each old file is
OLD_PATH = {"video_intake": "coordinator/video_intake.py", "delegation_contract": "coordinator/delegation_contract.py",
            "delegate_local": "coordinator/scripts/delegate_local.py", "textdiff": "coordinator/scripts/textdiff.py",
            "knowledge": "librarian/knowledge.py", "research_client": "librarian/research_client.py",
            "work_client": "engineer/work_client.py"}
AGENT_ENTRIES = {"coordinator/telegram.py": "agents/coordinator.py", "librarian/telegram.py": "agents/librarian.py",
                 "engineer/telegram.py": "agents/engineer.py", "private/telegram.py": "agents/private.py"}
TOP = set(GROUP_OF) | set(RENAMED)


def dotted(module):
    if module in RENAMED:
        return "tabularium.%s.%s" % RENAMED[module]
    return f"tabularium.{GROUP_OF[module]}.{module}"


def git(*args):
    subprocess.run(["git", *args], cwd=ROOT, check=True)


def move_files():
    moves = {}
    for module, group in GROUP_OF.items():
        old = OLD_PATH.get(module) or (f"agent_core/{module}.py" if (ROOT / "agent_core" / f"{module}.py").exists() else f"{module}.py")
        moves[old] = f"{PKG}/{group}/{module}.py"
    for old, (group, new) in RENAMED.items():
        moves[f"{old}.py"] = f"{PKG}/{group}/{new}.py"
    for old, new in AGENT_ENTRIES.items():
        moves[old] = f"{PKG}/{new}"
    moves["agent_core/benchmark_cases.json"] = f"{PKG}/learning/benchmark_cases.json"
    moves["search_order.json"] = f"{PKG}/tools/search_order.json"
    for rel in ("app.js", "messaging.js", "index.html", "style.css", "manifest.webmanifest"):
        moves[f"dashboard/{rel}"] = f"{PKG}/dashboard/static/{rel}"
    for p in (ROOT / "dashboard" / "assets").iterdir():
        moves[f"dashboard/assets/{p.name}"] = f"{PKG}/dashboard/static/assets/{p.name}"
    for p in sorted((ROOT / "dashboard" / "android").rglob("*")):
        if p.is_file():
            rel = p.relative_to(ROOT).as_posix()
            moves[rel] = f"{PKG}/dashboard/android/{p.relative_to(ROOT / 'dashboard' / 'android').as_posix()}"
    for p in sorted((ROOT / "coordinator" / "tests").glob("*.py")):
        assert not (ROOT / "tests" / p.name).exists(), p.name
        moves[f"coordinator/tests/{p.name}"] = f"tests/{p.name}"
    for old, new in moves.items():
        assert (ROOT / old).exists(), old
        (ROOT / new).parent.mkdir(parents=True, exist_ok=True)
        git("mv", old, new)
    git("rm", "-q", "agent_core/__init__.py")
    for group in list(GROUPS) + ["dashboard", "agents", ""]:
        init = ROOT / PKG / group / "__init__.py"
        if not init.exists():
            init.write_text("", encoding="utf-8")
    (ROOT / PKG / "__init__.py").write_text('"""Tabularium: an owner-judged agent framework."""\n__version__ = "0.4.0.dev0"\n', encoding="utf-8")
    return moves


NAMES = "|".join(sorted(TOP, key=len, reverse=True))
AGENT_CORE_FROM = re.compile(r"^(\s*)from agent_core import ([^\n(]+?)\s*(#.*)?$", re.M)
AGENT_CORE_SUB = re.compile(r"^(\s*)from agent_core\.(\w+) import (.+)$", re.M)
FROM_TOP = re.compile(rf"^(\s*)from ({NAMES}) import (.+)$", re.M)
IMPORT_TOP = re.compile(rf"^(\s*)import ({NAMES})(?: as (\w+))?\s*(#.*)?$", re.M)
IMPORT_TWO = re.compile(rf"^(\s*)import ({NAMES}), ({NAMES})\s*$", re.M)
DOTTED_CORE = re.compile(r"\bagent_core\.(\w+)")
PATCH_TOP = re.compile(rf"(patch(?:\.object)?\(\s*['\"])({NAMES})\.")


def rewrite_text(text):
    def core_from(m):
        indent, names, comment = m.group(1), m.group(2), m.group(3) or ""
        lines = []
        for part in [p.strip() for p in names.split(",") if p.strip()]:
            name = part.split(" as ")[0].strip()
            lines.append(f"{indent}from tabularium.{GROUP_OF[name]} import {part}")
        if comment:
            lines[-1] += "   " + comment
        return "\n".join(lines)

    def import_top(m):
        indent, name, alias, comment = m.group(1), m.group(2), m.group(3), m.group(4) or ""
        if name in RENAMED:
            group, new = RENAMED[name]
            line = f"{indent}from tabularium.{group} import {new} as {alias or name}"
        else:
            line = f"{indent}from tabularium.{GROUP_OF[name]} import {name}" + (f" as {alias}" if alias else "")
        return line + ("   " + comment if comment else "")

    text = AGENT_CORE_FROM.sub(core_from, text)
    text = AGENT_CORE_SUB.sub(lambda m: f"{m.group(1)}from {dotted(m.group(2))} import {m.group(3)}", text)
    text = IMPORT_TWO.sub(lambda m: import_top(re.match(IMPORT_TOP, f"{m.group(1)}import {m.group(2)}")) + "\n"
                          + import_top(re.match(IMPORT_TOP, f"{m.group(1)}import {m.group(3)}")), text)
    text = FROM_TOP.sub(lambda m: f"{m.group(1)}from {dotted(m.group(2))} import {m.group(3)}", text)
    text = IMPORT_TOP.sub(import_top, text)
    text = DOTTED_CORE.sub(lambda m: dotted(m.group(1)) if m.group(1) in GROUP_OF else m.group(0), text)
    text = PATCH_TOP.sub(lambda m: f"{m.group(1)}{dotted(m.group(2))}.", text)
    return text


SYS_PATH_LINE = re.compile(r"^\s*sys\.path(?:\.insert\(0, |\[:0\] ?= ?\[|\[:0\]=\[)[^\n]*$\n?", re.M)


def rewrite_files():
    for path in sorted((ROOT / "src").rglob("*.py")) + sorted((ROOT / "tests").rglob("*.py")):
        raw = path.read_bytes()
        crlf = b"\r\n" in raw
        text = raw.decode("utf-8-sig").replace("\r\n", "\n")
        new = rewrite_text(text)
        if path.parent == ROOT / "tests" and path.name not in ("agent_harness.py", "test_entry_imports.py"):
            new = SYS_PATH_LINE.sub("", new)
        if new != text:
            path.write_bytes(new.replace("\n", "\r\n").encode("utf-8") if crlf else new.encode("utf-8"))


def leftovers():
    pattern = re.compile(rf"^\s*(from|import) (agent_core|{NAMES})\b", re.M)
    found = []
    for path in sorted((ROOT / "src").rglob("*.py")) + sorted((ROOT / "tests").rglob("*.py")):
        for i, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            if pattern.match(line):
                found.append(f"{path.relative_to(ROOT)}:{i}: {line.strip()}")
    return found


if __name__ == "__main__":
    if (ROOT / PKG).exists():
        sys.exit("already restructured")
    moves = move_files()
    rewrite_files()
    print(f"moved {len(moves)} files")
    rest = leftovers()
    print("\n".join(rest) if rest else "no old-style imports remain")
