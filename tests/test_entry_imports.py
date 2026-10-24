"""Every script an agent can run as its own process finds the shared code (2026-10-15).

Why: the 2026-10-14 clean-up deleted the per-agent shim files that re-exported shared modules. Anything started as
`python <agent>/<script>.py` had found background_runtime and friends through those shims; Vit's work_client.py and
Cal's research_client.py then failed with ModuleNotFoundError, and every hand-off from Cato to Vit or Cal failed until
Cato noticed. The equivalence check only loaded the agents in-process, where D:\\Agents is already on the path.
Design: D:\\Agents is declared importable once, in the environment (site-packages/agents.pth), instead of each
entry point inserting paths by hand. This test resolves every module-level import of every agent-folder script the
way `python script.py` would (script folder first, then the interpreter's own path, PYTHONPATH ignored), in a fresh
process, without running the scripts. Only imports of shared modules (the D:\\Agents root files, agent_core) are checked; imports
inside try blocks are optional and skipped."""
import ast, json, subprocess, sys, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
AGENTS = ("cato", "tacitus", "callimachus", "vitruvius")
SCRIPTS = sorted(p for a in AGENTS for p in (*(ROOT / a).glob("*.py"), *(ROOT / a / "scripts").glob("*.py")))
SHARED = {p.stem for p in ROOT.glob("*.py")} | {"agent_core"}          # third-party packages are each script's own business

RESOLVE = """
import importlib.util, json, sys
base = list(sys.path)
missing = {}
for path, folder, modules in json.loads(sys.stdin.read()):
    sys.path[:] = [folder] + base
    sys.path_importer_cache.clear()
    gone = [m for m in modules if importlib.util.find_spec(m) is None]
    if gone: missing[path] = gone
print(json.dumps(missing))
"""


def required_imports(tree):
    """Top-level module names imported at module level, outside try blocks."""
    found = set()
    def visit(nodes):
        for n in nodes:
            if isinstance(n, ast.Import): found.update(a.name.split(".")[0] for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.level == 0: found.add(n.module.split(".")[0])
            elif isinstance(n, ast.If): visit(n.body); visit(n.orelse)
    visit(tree.body)
    return sorted(found & SHARED)


class EntryImports(unittest.TestCase):
    def test_every_agent_script_resolves_its_imports_as_its_own_process(self):
        self.assertIn(ROOT / "vitruvius" / "work_client.py", SCRIPTS)
        self.assertIn(ROOT / "callimachus" / "research_client.py", SCRIPTS)
        jobs = [(str(p), str(p.parent), required_imports(ast.parse(p.read_text(encoding="utf-8-sig")))) for p in SCRIPTS]
        out = subprocess.run([sys.executable, "-I", "-c", RESOLVE], input=json.dumps(jobs), capture_output=True,
                             text=True, cwd=str(ROOT.parent), timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), {})


if __name__ == "__main__":
    unittest.main()
