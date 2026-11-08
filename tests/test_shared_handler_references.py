"""No undefined names anywhere in the runtime (2026-10-11).

Why: the 2026-10-11 dedup moved 21 handlers into agent_core/shared_handlers.SharedHandlers and left two references per
agent bare (`{_language_hint(text)}` in an f-string, `_kill_headless_outlook` as a callback); every foreground message
failed with NameError for sixteen minutes (tasks 901-903). The same review found `_background` bare in Cato's delegate
tool since 2026-10-10, waiting for the next accept. Python only resolves a global name when the line runs, so a test
that executes some paths proves nothing about the rest. This test resolves every name statically, pyflakes-style: a
Load of a name that no scope in the file binds and that is not a builtin fails, for every runtime file and every agent.
Rebinding through module __getattr__ or exec would hide a name from this check; neither is used (see
test_namespace_integrity)."""
import ast, builtins, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
FILES = [ROOT / "engine.py", ROOT / "background_runtime.py", ROOT / "subscription_runtime.py", ROOT / "supervisor.py",
         ROOT / "agent_control.py", ROOT / "dashboard.py", ROOT / "task_cost.py", ROOT / "retention.py", ROOT / "agent_logging.py",
         *sorted((ROOT / "agent_core").glob("*.py")),
         *(ROOT / agent / "telegram.py" for agent in ("cato", "tacitus", "callimachus", "vitruvius"))]
BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__spec__", "__builtins__"}


def bound_names(tree):
    """Every name the module binds in any scope: defs, classes, parameters, imports, assignments, except-as, global."""
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)): names.add(n.name)
        elif isinstance(n, ast.arg): names.add(n.arg)
        elif isinstance(n, (ast.Import, ast.ImportFrom)): names |= {a.asname or a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)): names.add(n.id)
        elif isinstance(n, ast.ExceptHandler) and n.name: names.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)): names |= set(n.names)
    return names


def undefined(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    known = bound_names(tree) | BUILTINS
    return sorted({(n.id, n.lineno) for n in ast.walk(tree)
                   if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in known})


class UndefinedNames(unittest.TestCase):
    def test_every_loaded_name_is_bound_somewhere(self):
        problems = {str(p.relative_to(ROOT)): undefined(p) for p in FILES if undefined(p)}
        self.assertEqual(problems, {}, f"names used but never bound (NameError waiting to happen): {problems}")

    def test_catches_the_october_regressions(self):
        broken = ROOT / "backups" / "hotfix-language-hint-20261011-151123" / "cato-telegram.py"
        if broken.exists():
            self.assertEqual([n for n, _ in undefined(broken)], ["_background", "_background", "_background", "_background", "_kill_headless_outlook", "_language_hint"])


if __name__ == "__main__":
    unittest.main()
