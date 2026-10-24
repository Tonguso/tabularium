"""Roles are declared once and passed explicitly; agent lists are not hand-kept (2026-10-15).

Why: TaskStore used to infer delegation, budget and worker rights from its folder name, and the agent list was typed in
eight files; the operations view listed Cato and Tacitus only, so Cal and Vit never appeared in it. Now agent_core.roster
is the one table and every other file imports it."""
import ast, unittest
from pathlib import Path
from agent_core.roster import AGENTS, ROLES, Role
import background_runtime as rt
ROOT = Path(__file__).resolve().parents[1]
RUNTIME_FILES = [ROOT / n for n in ("engine.py", "background_runtime.py", "supervisor.py", "agent_control.py", "dashboard.py",
                                    "retention.py", "subscription_usage.py", "subscription_connections.py")] + sorted((ROOT / "agent_core").glob("*.py"))


class Roster(unittest.TestCase):
    def test_roles_are_explicit_not_inferred_from_the_folder(self):
        store = rt.TaskStore(ROOT / "callimachus")                       # no role passed: Cato's default, whatever the folder says
        self.assertEqual(store.role, ROLES["cato"])
        store = rt.TaskStore(ROOT / "anything", ROLES["callimachus"])
        self.assertEqual((store.role.delegates, store.role.owner_budget, store.role.request_table, store.worker_limit()),
                         (False, False, "research_requests", 2))
        self.assertEqual(ROLES["vitruvius"].env_var, "VITRUVIUS_BACKGROUND_TASK")
        self.assertEqual(AGENTS, ("cato", "tacitus", "callimachus", "vitruvius"))
        self.assertTrue(all(isinstance(r, Role) and r.name == n for n, r in ROLES.items()))

    def test_no_other_file_keeps_its_own_agent_list(self):
        offenders = []
        for path in RUNTIME_FILES:
            if path.name == "roster.py":
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
                if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
                    names = {n.value for n in node.elts if isinstance(n, ast.Constant) and isinstance(n.value, str)}
                    if {"cato", "callimachus", "vitruvius"} <= names or {"cato", "tacitus"} == names:
                        offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
