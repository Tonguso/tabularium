"""Roles are declared once and passed explicitly; agent lists are not hand-kept (2026-10-15).

Why: TaskStore used to infer delegation, budget and worker rights from its folder name, and the agent list was typed in
eight files; the operations view listed Cato and Tacitus only, so Cal and Vit never appeared in it. Now tabularium.core.roster
is the one table and every other file imports it."""
import ast, tempfile, unittest
from pathlib import Path
from tabularium.core.roster import AGENTS, ROLES, Role
from tabularium.records import background_runtime as rt
ROOT = Path(__file__).resolve().parents[1]
RUNTIME_FILES = sorted(p for p in (ROOT / "src" / "tabularium").rglob("*.py") if p.parts[-2] not in ("agents", "templates"))


class Roster(unittest.TestCase):
    def test_roles_are_explicit_not_inferred_from_the_folder(self):
        store = rt.TaskStore(ROOT / "librarian")                       # no role passed: Cato's default, whatever the folder says
        self.assertEqual(store.role, ROLES["coordinator"])
        with tempfile.TemporaryDirectory() as tmp:                      # never a folder inside the repository
            store = rt.TaskStore(Path(tmp) / "anything", ROLES["librarian"])
        self.assertEqual((store.role.delegates, store.role.owner_budget, store.role.request_table, store.worker_limit()),
                         (False, False, "research_requests", 2))
        self.assertEqual(ROLES["engineer"].env_var, "ENGINEER_BACKGROUND_TASK")
        self.assertEqual(AGENTS, ("coordinator", "private", "librarian", "engineer"))
        self.assertTrue(all(isinstance(r, Role) and r.name == n for n, r in ROLES.items()))

    def test_no_other_file_keeps_its_own_agent_list(self):
        offenders = []
        for path in RUNTIME_FILES:
            if path.name == "roster.py":
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
                if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
                    names = {n.value for n in node.elts if isinstance(n, ast.Constant) and isinstance(n.value, str)}
                    if {"coordinator", "librarian", "engineer"} <= names or {"coordinator", "private"} == names:
                        offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
