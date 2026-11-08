"""Every module of the package imports, and nothing imports the flat layout's names (2026-11-08).

Why: the restructure moved sixty modules into one package and rewrote their imports by script. A module that still
says `import engine` would work on a machine whose environment also carries the old checkout and fail everywhere
else. This imports every module in a fresh process with only src/ on the path, so the package stands alone."""
import json
import os
import pkgutil
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
OLD = re.compile(r"^\s*(?:from|import) (?:agent_core|engine|background_runtime|agent_runtime|agent_logging|agent_control|"
                 r"dashboard|dashboard_audit|dashboard_history|owner_telegram|provider_routing|retention|search_routing|"
                 r"spreadsheet_upload|subscription_connections|subscription_runtime|subscription_usage|supervisor|task_cost|"
                 r"delegation_contract|video_intake|knowledge|research_client|work_client|telegram)\b", re.M)
IMPORT_ALL = """
import importlib, json, os, pkgutil, sys
from pathlib import Path
sys.path[:] = [p for p in sys.path if not ((Path(p or ".") / "agent_core").is_dir() or (Path(p or ".") / "engine.py").is_file())]
sys.path.insert(0, os.environ["TABULARIUM_SRC"])
import tabularium
assert Path(tabularium.__file__).resolve().parent.parent == Path(os.environ["TABULARIUM_SRC"]).resolve(), tabularium.__file__
failed = {}
for info in pkgutil.walk_packages(tabularium.__path__, "tabularium."):
    if info.name.endswith(".__main__"):
        continue
    try:
        importlib.import_module(info.name)
    except Exception as exc:
        failed[info.name] = f"{type(exc).__name__}: {exc}"
print(json.dumps(failed))
"""


class PackageImports(unittest.TestCase):
    def test_every_module_imports_with_only_src_on_the_path(self):
        out = subprocess.run([sys.executable, "-X", "utf8", "-c", IMPORT_ALL], capture_output=True, text=True,
                             cwd=str(ROOT), timeout=300,
                             env=dict(os.environ, TABULARIUM_SRC=str(SRC), TABULARIUM_HOME=str(ROOT)))
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertEqual(json.loads(out.stdout.strip().splitlines()[-1]), {})

    def test_no_module_imports_the_flat_layout(self):
        offenders = []
        for path in sorted(SRC.rglob("*.py")) + sorted((ROOT / "tests").rglob("*.py")):
            for i, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
                if OLD.match(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{i}: {line.strip()}")
        self.assertEqual(offenders, [])

    def test_the_roster_names_a_role_module_for_every_slot(self):
        sys.path.insert(0, str(SRC))
        from tabularium.core.roster import ROLES
        for name, role in ROLES.items():
            self.assertTrue((SRC / "tabularium" / "agents" / f"{role.entry}.py").is_file(), name)


if __name__ == "__main__":
    unittest.main()
