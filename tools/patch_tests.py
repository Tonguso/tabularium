"""One-off: tests that read source files or start processes by the flat layout's paths follow the package."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
T = ROOT / "tests"
PKG = 'ROOT / "src" / "tabularium"'


def patch(name, *pairs, regex=()):
    path = T / name
    text = path.read_text(encoding="utf-8")
    for old, new in pairs:
        assert text.count(old) == 1, (name, old[:70], text.count(old))
        text = text.replace(old, new)
    for pattern, repl in regex:
        text, n = re.subn(pattern, repl, text, flags=re.M | re.S)
        assert n >= 1, (name, pattern)
    path.write_text(text, encoding="utf-8")


ROLE_FILES = '*(ROOT / "src" / "tabularium" / "agents" / f"{m}.py" for m in ("coordinator", "private", "librarian", "engineer"))'

patch("test_always_skills.py",
      ('src = (ROOT / "private" / "telegram.py").read_text(encoding="utf-8")',
       'src = (ROOT / "src" / "tabularium" / "agents" / "private.py").read_text(encoding="utf-8")'))
patch("test_budget_enforcement.py",
      ('src = (ROOT.parent / "background_runtime.py").read_text(encoding="utf-8")',
       'src = (ROOT / "src" / "tabularium" / "records" / "background_runtime.py").read_text(encoding="utf-8")'),
      ('for client in (ROOT.parent / "librarian" / "research_client.py", ROOT.parent / "engineer" / "work_client.py"):',
       'for client in (ROOT / "src" / "tabularium" / "delegation" / "research_client.py", ROOT / "src" / "tabularium" / "delegation" / "work_client.py"):'))
patch("test_one_turn_loop.py",
      ('FILES = [ROOT / "engine.py", ROOT / "subscription_runtime.py", *sorted((ROOT / "agent_core").glob("*.py"))]',
       'FILES = sorted(p for p in (ROOT / "src" / "tabularium").rglob("*.py") if p.parts[-2] != "agents")'))
patch("test_outcomes.py",
      ('FILES = [ROOT / "engine.py", ROOT / "background_runtime.py", *sorted((ROOT / "agent_core").glob("*.py")),\n'
       '         *(ROOT / a / "telegram.py" for a in ("coordinator", "private", "librarian", "engineer"))]',
       'FILES = sorted((ROOT / "src" / "tabularium").rglob("*.py"))'))
patch("test_roster.py",
      ('RUNTIME_FILES = [ROOT / n for n in ("engine.py", "background_runtime.py", "supervisor.py", "agent_control.py", "dashboard.py",\n'
       '                                    "retention.py", "subscription_usage.py", "subscription_connections.py")] + sorted((ROOT / "agent_core").glob("*.py"))',
       'RUNTIME_FILES = sorted(p for p in (ROOT / "src" / "tabularium").rglob("*.py") if p.parts[-2] not in ("agents", "templates"))'))
patch("test_search_order.py",
      ('src = (ROOT / "engine.py").read_text(encoding="utf-8")', 'src = (ROOT / "src" / "tabularium" / "core" / "engine.py").read_text(encoding="utf-8")'),
      ('app = (ROOT / "agent_core" / "agent_app.py").read_text(encoding="utf-8")', 'app = (ROOT / "src" / "tabularium" / "core" / "agent_app.py").read_text(encoding="utf-8")'),
      ('        for agent in AGENTS:\n            src = (ROOT / agent / "telegram.py").read_text(encoding="utf-8")\n            self.assertNotIn("SEARCH_PROVIDER", src, agent)',
       '        for agent in (ROOT / "src" / "tabularium" / "agents").glob("*.py"):\n            self.assertNotIn("SEARCH_PROVIDER", agent.read_text(encoding="utf-8"), agent.name)'),
      ('src = (ROOT / "dashboard.py").read_text(encoding="utf-8")', 'src = (ROOT / "src" / "tabularium" / "dashboard" / "server.py").read_text(encoding="utf-8")'),
      ('(ROOT / "dashboard" / "index.html").read_text', '(ROOT / "src" / "tabularium" / "dashboard" / "static" / "index.html").read_text'),
      ('(ROOT / "dashboard" / "app.js").read_text', '(ROOT / "src" / "tabularium" / "dashboard" / "static" / "app.js").read_text'))
patch("test_shared_handler_references.py",
      ('FILES = [ROOT / "engine.py", ROOT / "background_runtime.py", ROOT / "subscription_runtime.py", ROOT / "supervisor.py",\n'
       '         ROOT / "agent_control.py", ROOT / "dashboard.py", ROOT / "task_cost.py", ROOT / "retention.py", ROOT / "agent_logging.py",\n'
       '         *sorted((ROOT / "agent_core").glob("*.py")),\n'
       '         *(ROOT / agent / "telegram.py" for agent in ("coordinator", "private", "librarian", "engineer"))]',
       'FILES = sorted((ROOT / "src" / "tabularium").rglob("*.py"))'))
patch("test_superseded_rule.py",
      ('hits = [p.name for p in (ROOT / "agent_core").glob("*.py") if p.name != "memory_store.py"',
       'hits = [p.name for p in (ROOT / "src" / "tabularium").rglob("*.py") if p.name != "memory_store.py"'))
patch("test_stall.py",
      ('(ROOT / "subscription_runtime.py").read_text', '(ROOT / "src" / "tabularium" / "models" / "subscription_runtime.py").read_text'))
patch("test_work_method.py",
      ('for rel in ("engine.py", "coordinator/telegram.py"):', 'for rel in ("src/tabularium/core/engine.py", "src/tabularium/agents/coordinator.py"):'),
      ('r = subprocess.run([sys.executable, str(ROOT / "coordinator" / "scripts" / "textdiff.py"), str(a), str(b), "--srt"],',
       'r = subprocess.run([sys.executable, "-m", "tabularium.tools.textdiff", str(a), str(b), "--srt"],'))
patch("test_session_ownership.py",
      ("        runtime = str(Path(__file__).resolve().parents[1] / 'background_runtime.py')   # not D:/Agents: the public export runs elsewhere (2026-10-24)\n"
       "        proc = subprocess.run([sys.executable,'-B',runtime,'--worker',str(ident),",
       "        proc = subprocess.run([sys.executable,'-B','-m','tabularium','worker',str(ident),"))
patch("test_restart_evidence.py",
      ("'CommandLine': 'D:\\\\Agents\\\\coordinator\\\\telegram.py'}", "'CommandLine': 'python.exe -m tabularium run coordinator'}"))
patch("test_release_flow.py",
      ("self.assertEqual([Path(c[0]).name for c in calls],['python.exe','python.exe'])", "self.assertEqual([Path(c[0]).name for c in calls],['python.exe'])"))
patch("test_current_state.py",
      ('self.assertEqual(popen.call_args.args[0][2:4], ["eval", "coordinator"])', 'self.assertEqual(popen.call_args.args[0][1:5], ["-m", "tabularium", "eval", "coordinator"])'))
patch("test_dashboard_media.py",
      ("        from coordinator import video_intake\n", "        from tabularium.tools import video_intake\n"))
patch("test_smoke_agents.py",
      ('"""Offline: each agent\'s telegram.py loads in a fresh interpreter on top of engine.py (fake credentials, temp\n',
       '"""Offline: each agent builds in a fresh interpreter from its role module (fake credentials, temp\n'),
      ('src = (agent_dir / "telegram.py").read_text(encoding="utf-8")\nmarker = "AGENT_DIR   = Path(__file__).resolve().parent"\n'
       'assert marker in src, "AGENT_DIR line changed"\nsrc = src.replace(marker, "AGENT_DIR   = Path(" + repr(str(tmp)) + ")")\n', ''),
      ('scope = {"__file__": str(agent_dir / "telegram.py"), "__name__": f"isolated_{a}"}\nexec(compile(src, str(agent_dir / "telegram.py"), "exec"), scope)\n',
       'from tabularium.agents import build\nruntime = build(tmp, a)\n'),
      ("runtime=scope['runtime']\n", ""))
# Scripted workers: the stub is the folder's own agent module now.
for name in ("test_requirement_efficiency.py", "test_session_ownership.py", "test_task_decisions.py", "test_trials.py",
             "test_waiting_handoff.py", "test_wire_budget.py", "test_worker.py"):
    path = T / name
    text = path.read_text(encoding="utf-8")
    new = re.sub(r"(['\"])telegram\.py(['\"])", r"\1agent.py\2", text)
    assert new != text, name
    path.write_text(new, encoding="utf-8")
print("tests patched")
