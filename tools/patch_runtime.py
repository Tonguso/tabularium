"""One-off: every place the runtime assumed that code and state share one folder now goes through tabularium.paths,
and every place that started a Python file by path now starts the package's entry point."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src/tabularium"
HOME_IMPORT = "from tabularium.paths import home as _home\n"


def patch(rel, *pairs, add_import=None, regex=()):
    path = SRC / rel
    text = path.read_text(encoding="utf-8")
    if pairs and pairs[0][0] not in text and pairs[0][1] in text:
        print("already patched", rel)
        return
    for old, new in pairs:
        assert text.count(old) == 1, (rel, old[:60], text.count(old))
        text = text.replace(old, new)
    for pattern, repl in regex:
        text, n = re.subn(pattern, repl, text, flags=re.M)
        assert n >= 1, (rel, pattern)
    if add_import and add_import not in text:
        # after the module docstring, before the first import
        m = re.search(r'^(?:"""[\s\S]*?"""\n|#[^\n]*\n|\s*\n)*', text)
        text = text[:m.end()] + add_import + text[m.end():]
    path.write_text(text, encoding="utf-8")


# --- background workers -------------------------------------------------------------------------------------------
patch("records/background_runtime.py",
      ('sys.path.insert(0, str(Path(__file__).resolve().parent))\n', ''),
      ('argv = [sys.executable, str(Path(__file__).resolve()), "--worker", str(ident), "--agent", str(self.agent_dir),\n'
       '                        "--role", self.role.name, "--attempt", row["attempt"]]',
       'argv = [sys.executable, "-m", "tabularium", "worker", str(ident), "--agent", str(self.agent_dir),\n'
       '                        "--role", self.role.name, "--attempt", row["attempt"]]'),
      ('proc = subprocess.run([sys.executable, str(self.agent_dir / "scripts" / "delegate_local.py"), agent, "status",',
       'proc = subprocess.run([sys.executable, "-m", "tabularium.delegation.delegate_local", agent, "status",'),
      ('def worker(ident, agent_dir, role, attempt=None):\n    sys.path.insert(0, str(Path(agent_dir).resolve()))\n',
       'def worker(ident, agent_dir, role, attempt=None):\n'),
      ('            import importlib.util\n            from tabularium.learning import trials\n',
       '            from tabularium.learning import trials\n'),
      ('            spec = importlib.util.spec_from_file_location(f"{store.role.name}_worker", store.agent_dir / "telegram.py")\n'
       '            agent = importlib.util.module_from_spec(spec)\n'
       '            spec.loader.exec_module(agent)\n',
       '            from tabularium.agents import build\n'
       '            agent = build(store.agent_dir, store.role.name)\n'),
      ('            agent.runtime._background = store\n', '            agent._background = store\n'),
      ('                trials.scope(agent.runtime, task_context)\n', '                trials.scope(agent, task_context)\n'),
      ('            agent.runtime.SYSTEM_PROMPT = agent.SYSTEM_PROMPT + (', '            agent.SYSTEM_PROMPT = agent.SYSTEM_PROMPT + ('),
      ('                agent.runtime.SYSTEM_PROMPT = trials.worker_prompt(task_context)', '                agent.SYSTEM_PROMPT = trials.worker_prompt(task_context)'),
      ('Workers are started as `python background_runtime.py --worker <id> --agent <dir> --role <name>`.',
       'Workers are started as `tabularium worker <id> --agent <dir> --role <name>`.'),
      regex=[(r'\nif __name__ == "__main__":\n    # A worker process:[\s\S]*\Z', '\n')])

# --- supervisor -----------------------------------------------------------------------------------------------------
patch("ops/supervisor.py",
      ('ROOT = Path(__file__).resolve().parent\nPY = r"C:\\ProgramData\\miniconda3\\envs\\ai_agents\\python.exe"\n',
       'ROOT = _home()\n'
       'PY = str(Path(sys.executable).with_name("python.exe")) if sys.platform == "win32" else sys.executable\n'),
      ('    if name == "dashboard":\n        return [PY, str(ROOT / "dashboard.py")]\n    return ["cmd.exe", "/d", "/c", str(ROOT / "run_agent.bat"), name]\n',
       '    if name == "dashboard":\n        return [PY, "-m", "tabularium", "dashboard"]\n    return [PY, "-m", "tabularium", "run", name]\n'),
      ('                hit = name.startswith("python") and "dashboard.py" in cmd\n            else:\n'
       '                hit = (name == "cmd.exe" and \'run_agent.bat" \' + child.name in cmd) or \\\n'
       '                      (name.startswith("python") and child.name + "\\\\telegram.py" in cmd)\n',
       '                hit = name.startswith("python") and "-m tabularium dashboard" in cmd\n            else:\n'
       '                hit = name.startswith("python") and "-m tabularium run " + child.name in cmd\n'),
      ('    # run_agent.bat appends the agent\'s own output to this log itself; holding the file open here made cmd\'s\n'
       '    # redirection fail with a sharing violation. The dashboard has no launcher, so its output goes to the log directly.\n'
       '    if child.name == "dashboard":\n        out = log.open("ab")\n    else:\n        out = subprocess.DEVNULL\n',
       '    out = log.open("ab")\n'),
      ('    env = dict(os.environ, AGENT_LAUNCH_ID=child.instance)\n',
       '    env = dict(os.environ, AGENT_LAUNCH_ID=child.instance, TABULARIUM_HOME=str(ROOT))\n'),
      ('    finally:\n        if out is not subprocess.DEVNULL:\n            out.close()\n', '    finally:\n        out.close()\n'),
      ('        config = (ROOT / "config.bat").read_text(encoding="utf-8-sig")\n'
       '        match = re.search(r\'^\\s*set\\s+"?TELEGRAM_BOT_TOKEN=([^\\r\\n"]+)\', config, re.I | re.M)\n'
       '        if not match:\n            return\n',
       '        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()\n        if not token:\n            return\n'),
      ('"https://api.telegram.org/bot" + match.group(1).strip() + "/sendMessage"', '"https://api.telegram.org/bot" + token + "/sendMessage"'),
      ('    sys.path.insert(0, str(ROOT))\n    from tabularium.core.agent_logging import start_logging\n',
       '    from tabularium.core.agent_logging import start_logging\n'),
      ('    import re, urllib.request\n', '    import urllib.request\n'),
      add_import=HOME_IMPORT)

# --- dashboard ------------------------------------------------------------------------------------------------------
patch("dashboard/server.py",
      ('ROOT = Path(__file__).resolve().parent\n', 'ROOT = _home()\nSTATIC = Path(__file__).resolve().parent\n'),
      ("            static['/agents.apk']=('android/build/agents.apk','application/vnd.android.package-archive')\n",
       "            static['/agents.apk']=('android/build/agents.apk','application/vnd.android.package-archive')\n"),
      ("            if not (ROOT/'dashboard/assets/librarian.jpg').exists():\n", "            if not (STATIC/'static/assets/librarian.jpg').exists():\n"),
      ("            if not (ROOT/'dashboard/assets/engineer.jpg').exists():\n", "            if not (STATIC/'static/assets/engineer.jpg').exists():\n"),
      ("                filename,kind=static[url.path];self.send((ROOT/'dashboard'/filename).read_bytes(),kind)\n",
       "                filename,kind=static[url.path]\n"
       "                self.send((STATIC/('static/'+filename if not filename.startswith('android/') else filename)).read_bytes(),kind)\n"),
      add_import=HOME_IMPORT)
patch("dashboard/history.py", ("ROOT=Path(__file__).resolve().parent\n", "ROOT=_home()\n"), add_import=HOME_IMPORT)
patch("dashboard/audit.py",
      ("import sys,json,re\nfrom pathlib import Path\nsys.path.insert(0,str(Path(__file__).parent/'coordinator'/'scripts'))\n",
       "import json,re\nfrom pathlib import Path\nfrom tabularium.paths import home as _home\n"),
      ("    path=Path(__file__).parent/'coordinator'/'task_state'/'background'/'jobs.sqlite3'\n", "    path=_home()/'coordinator'/'task_state'/'background'/'jobs.sqlite3'\n"))

# --- models, tools, ops ---------------------------------------------------------------------------------------------
patch("models/subscription_connections.py", ("ROOT = Path(__file__).resolve().parent\n", "ROOT = _home()\n"), add_import=HOME_IMPORT)
patch("models/subscription_usage.py", ("ROOT=Path(__file__).resolve().parent\n", "ROOT=_home()\n"), add_import=HOME_IMPORT)
patch("models/subscription_runtime.py",
      ("    PATH = Path(__file__).resolve().parent / 'logs' / 'claude_cli_start.lock'\n", "    PATH = _home() / 'logs' / 'claude_cli_start.lock'\n"),
      add_import=HOME_IMPORT)
patch("tools/search_routing.py",
      ('ORDER_FILE = Path(__file__).resolve().parent / "search_order.json"\n',
       'DEFAULT_ORDER_FILE = Path(__file__).resolve().parent / "search_order.json"     # the package default\n'
       'ORDER_FILE = _home() / "search_order.json"                                      # the installation\'s own order\n'),
      ('        order = json.loads(ORDER_FILE.read_text(encoding="utf-8")).get("order") or []\n',
       '        path = ORDER_FILE if ORDER_FILE.exists() else DEFAULT_ORDER_FILE\n'
       '        order = json.loads(path.read_text(encoding="utf-8")).get("order") or []\n'),
      add_import=HOME_IMPORT)
patch("ops/retention.py", ('ROOT = Path(__file__).resolve().parent\n', 'ROOT = _home()\n'), add_import=HOME_IMPORT)
patch("ops/release.py",
      ('ROOT = Path(__file__).resolve().parents[1]\n', 'ROOT = _home()\n'),
      ('    result = subprocess.run([\'git\', \'-C\', str(root), \'ls-files\', \'-z\'], capture_output=True, timeout=30, creationflags=getattr(subprocess, \'CREATE_NO_WINDOW\', 0))\n'
       '    if result.returncode:\n        raise RuntimeError(\'Cannot identify release source\')\n',
       '    try:\n'
       '        result = subprocess.run([\'git\', \'-C\', str(root), \'ls-files\', \'-z\'], capture_output=True, timeout=30, creationflags=getattr(subprocess, \'CREATE_NO_WINDOW\', 0))\n'
       '    except OSError:\n        result = None\n'
       '    if result is None or result.returncode:\n        return {\'commit\': \'\', \'digest\': \'\'}          # not a Git checkout: no release identity, no releases\n'),
      ("        if not name or name.startswith(('docs/', 'tests/', 'coordinator/tests/', 'public/')):\n",
       "        if not name or name.startswith(('docs/', 'tests/', 'public/')):\n"),
      ("    for directory in ('tests', 'coordinator/tests'):\n", "    for directory in ('tests',):\n"),
      ('"""Which code a worker ran: the Git commit of D:\\\\Agents (2026-10-21).', '"""Which code a worker ran: the Git commit of the home folder (2026-10-21).'),
      add_import=HOME_IMPORT)
patch("core/agent_logging.py",
      ('LOG_DIR = Path(r"D:\\Agents\\logs")\n', 'LOG_DIR = _logs()\n'),
      ('    import sys\n    from pathlib import Path\n    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # D:\\\\Agents\n'
       '    from tabularium.core.agent_logging import start_logging\n    start_logging("coordinator")\n',
       '    from tabularium.core.agent_logging import start_logging\n    start_logging("coordinator")\n'),
      ('This tees stdout/stderr to D:\\\\Agents\\\\logs\\\\<name>.log', 'This tees stdout/stderr to <home>/logs/<name>.log'),
      add_import="from tabularium.paths import logs as _logs\n")
patch("core/agent_app.py",
      ('        self.SYSTEM_PROMPT += "\\n\\n" + (Path(__file__).resolve().parent.parent / "POLICY.md").read_text(encoding="utf-8")\n',
       '        self.SYSTEM_PROMPT += "\\n\\n" + policy_text()\n'),
      ('run a script (e.g. scripts/textdiff.py) and read its output', 'run a script (e.g. python -m tabularium.tools.textdiff) and read its output'),
      ('    # ── Hooks for the agent subclass ─────────────────────────────────────────────\n',
       '    # ── Hooks for the agent subclass ─────────────────────────────────────────────\n'),
      regex=[(r'^(class AgentApp\()', 'def policy_text():\n    """The installation\'s POLICY.md (the owner\'s authority rules), or the package template when it has none."""\n'
                                      '    from tabularium.paths import home, PACKAGE\n'
                                      '    path = home() / "POLICY.md"\n'
                                      '    return (path if path.exists() else PACKAGE / "templates" / "POLICY.md").read_text(encoding="utf-8")\n\n\n\\1')])
patch("core/engine.py",
      ('        root = self.AGENT_DIR.parent\n        with open(root / "logs" / f"eval-{self.AGENT_DIR.name}.log", "ab") as log:   # the child keeps its own handle\n'
       '            subprocess.Popen([sys.executable, str(root / "agent_control.py"), "eval", self.AGENT_DIR.name, "--k", "3", "--sample", "8"],\n',
       '        root = self.AGENT_DIR.parent\n        with open(root / "logs" / f"eval-{self.AGENT_DIR.name}.log", "ab") as log:   # the child keeps its own handle\n'
       '            subprocess.Popen([sys.executable, "-m", "tabularium", "eval", self.AGENT_DIR.name, "--k", "3", "--sample", "8"],\n'))
patch("agents/coordinator.py",
      ('    def __init__(self, config, agent_dir):\n        self.DELEGATE_SCRIPT = agent_dir / "scripts" / "delegate_local.py"\n        super().__init__(config, agent_dir)\n',
       '    DELEGATE_ARGV = (sys.executable, "-m", "tabularium.delegation.delegate_local")\n'),
      ('            proc = run([sys.executable, str(self.DELEGATE_SCRIPT), agent, action, *args])\n',
       '            proc = run([*self.DELEGATE_ARGV, agent, action, *args])\n'),
      ('        """Wrap scripts/delegate_local.py (its credential checks and audit)', '        """Wrap tabularium.delegation.delegate_local (its credential checks and audit)'))
patch("delegation/delegate_local.py",
      ("ROOT = Path(__file__).resolve().parents[1]\nAGENTS = {'vit': ROOT.parent/'engineer', 'cal': ROOT.parent/'librarian'}\n"
       "CLIENTS = {'vit': AGENTS['vit']/'work_client.py', 'cal': ROOT/'scripts'/'cal_research.py'}\n",
       "ROOT = _home()/'coordinator'\nAGENTS = {'vit': _home()/'engineer', 'cal': _home()/'librarian'}\n"
       "CLIENTS = {'vit': 'tabularium.delegation.work_client', 'cal': 'tabularium.delegation.research_client'}\n"),
      ("    args=[sys.executable,str(CLIENTS[agent]),action]\n", "    args=[sys.executable,'-m',CLIENTS[agent],action]\n"),
      add_import=HOME_IMPORT)
patch("delegation/research_client.py", ("ROOT = Path(__file__).resolve().parent\n", "ROOT = _home()/'librarian'\n"), add_import=HOME_IMPORT)
patch("delegation/work_client.py", ("ROOT = Path(__file__).resolve().parent\n", "ROOT = _home()/'engineer'\n"), add_import=HOME_IMPORT)
patch("tools/knowledge.py", ("ROOT = Path(__file__).resolve().parent\n", "ROOT = _home()/'librarian'\n"), add_import=HOME_IMPORT)
patch("tools/video_intake.py", ('AGENT_DIR = Path(__file__).resolve().parent\n', "AGENT_DIR = _home()/'coordinator'\n"), add_import=HOME_IMPORT)
patch("delegation/delegation_contract.py", ("ROOT = Path(__file__).resolve().parent                      # D:\\Agents\\coordinator\n", "ROOT = _home()/'coordinator'\n"), add_import=HOME_IMPORT)
patch("learning/interaction_eval.py",
      ("ROOT = Path(__file__).resolve().parents[1]\n", "ROOT = _home()\n"),
      ("    files = ['telegram.py'] + {'coordinator':['video_intake.py','delegation_contract.py'],\n"
       "                              'librarian':['knowledge.py']}.get(name, [])\n"
       "    for filename in files:\n        shutil.copyfile(ROOT/name/filename, root/filename)\n", ""),
      ("    \"\"\"Copy entry-point CODE only; no live skill, memory, conversation, queue or credentials.\"\"\"\n",
       "    \"\"\"An empty agent folder for the role: no live skill, memory, conversation, queue or credentials.\"\"\"\n"),
      add_import=HOME_IMPORT,
      regex=[(r'def load_runtime\(root\):\n    """[^\n]*"""\n[\s\S]*?\n    agent = module\.runtime\n',
              'def load_runtime(root):\n    """Build the role\'s agent at its isolated path, without starting polling."""\n'
              '    root = Path(root)\n    from tabularium.agents import build\n    agent = build(root, root.name)\n')])
patch("ops/agent_control.py",
      ('if __name__ == "__main__":\n    import sys\n    args = sys.argv[1:]\n', 'def main(args):\n    import sys\n'),
      ("    root = Path(root or Path(__file__).resolve().parent)\n", "    root = Path(root or _home())\n"),
      ('    subprocess.Popen(["explorer.exe", str(Path(__file__).resolve().parent / "start_supervisor.bat")])\n'
       '    return "Task AgentsSupervisor is not installed (run \'Install Supervisor.bat\' once); started the supervisor directly instead."\n',
       '    pythonw = Path(sys.executable).with_name("pythonw.exe")\n'
       '    subprocess.Popen([str(pythonw if pythonw.exists() else sys.executable), "-m", "tabularium", "supervisor"], cwd=str(_home()),\n'
       '                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n'
       '                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))\n'
       '    return "Task AgentsSupervisor is not installed; started the supervisor directly instead."\n'),
      ('    icon = Path(__file__).resolve().parent / "dashboard" / "assets" / "tabularium.ico"\n',
       '    icon = Path(__file__).resolve().parents[1] / "dashboard" / "static" / "assets" / "tabularium.ico"\n'),
      ('    log = Path(__file__).resolve().parent / "logs" / "dashboard-window.log"\n', '    log = _home() / "logs" / "dashboard-window.log"\n'),
      ('        root = Path(__file__).resolve().parent\n        name = next(', '        root = _home()\n        name = next('),
      ('        root = Path(__file__).resolve().parent\n        for name in [a for a in args[1:] if not a.startswith("--")] or ["coordinator"]:',
       '        root = _home()\n        for name in [a for a in args[1:] if not a.startswith("--")] or ["coordinator"]:'),
      ('            flag = Path(__file__).resolve().parent / name / "task_state" / f"memory_{args[0]}_requested"\n',
       '            flag = _home() / name / "task_state" / f"memory_{args[0]}_requested"\n'),
      ('    import subprocess\n    if supervisor_alive():\n', '    import subprocess, sys\n    if supervisor_alive():\n'),
      add_import=HOME_IMPORT)
print("patched")
