"""One-off: turn the four entry-point modules into role modules with build(agent_dir, overrides)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENTS = {"coordinator": ("Cato", "CatoApp", "coordinator"), "librarian": ("Callimachus", "CallimachusApp", "librarian"),
          "engineer": ("Vitruvius", "VitruviusApp", "engineer"), "private": ("Tacitus", "TacitusApp", "private")}

HEADER = re.compile(r"# --- durable logging:.*?\n(?:.*\n)*?    _start_logging\(\"\w+\"\)\n", re.M)
AGENT_DIR = re.compile(r"^AGENT_DIR   = Path\(__file__\)\.resolve\(\)\.parent.*\n\n?", re.M)
TAIL = re.compile(r"\n*runtime = (\w+)\(CONFIG, AGENT_DIR\)\n\n\ndef __getattr__\(name\):.*\n    return getattr\(runtime, name\)\n\n\nif __name__ == \"__main__\":\n    runtime\.main\(\)\n")

for module, (display, app, slot) in AGENTS.items():
    path = ROOT / "src/tabularium/agents" / f"{module}.py"
    text = path.read_text(encoding="utf-8")
    if "def build(" in text:
        continue
    text, n = HEADER.subn("", text); assert n == 1, (module, "header")
    text, n = AGENT_DIR.subn("", text); assert n == 1, (module, "agent_dir")
    extra = ""
    if module == "engineer":                      # the thread limits move into build(), before the agent loads numpy
        extra = ("    import os\n    for limit in (\"OMP_NUM_THREADS\", \"OPENBLAS_NUM_THREADS\", \"MKL_NUM_THREADS\", \"NUMEXPR_NUM_THREADS\", \"VECLIB_MAXIMUM_THREADS\"):\n"
                 "        os.environ[limit] = \"1\"\n")
    tail = (f"\n\ndef build(agent_dir, overrides=None):\n"
            f"    \"\"\"The {display.lower() if module != 'private' else 'private'} agent for this folder (see tabularium.agents.build).\"\"\"\n"
            f"{extra}"
            f"    from tabularium.agents import configure\n"
            f"    return {app}(configure(CONFIG, overrides or {{}}), agent_dir)\n")
    text, n = TAIL.subn(tail, text); assert n == 1, (module, "tail")
    text = text.replace(f"Run:\n    python D:\\\\agents\\\\{slot}\\\\telegram.py\n", f"Run:\n    tabularium run {slot}\n")
    text = text.replace("agent_core/agent_app.py", "tabularium/core/agent_app.py")
    # the leftover private imports of the removed header
    text = re.sub(r"^import sys as _sys\n|^from pathlib import Path as _Path\n|^import os as _os\n", "", text, flags=re.M)
    path.write_text(text, encoding="utf-8")
    print("patched", module)
