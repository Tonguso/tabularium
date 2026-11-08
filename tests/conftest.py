"""Test configuration: the package under test is the one in src/, the home folder is the repository.

The tests import `tabularium` from src/ whether or not the package is installed, and they never pick up another
checkout that an environment file (a .pth) may have put on the path: any path entry that carries a flat-layout
`engine.py` or an `agent_core` package is removed first.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:] = [p for p in sys.path if not ((Path(p or ".") / "agent_core").is_dir() or (Path(p or ".") / "engine.py").is_file())]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("TABULARIUM_HOME", str(ROOT))
# Child interpreters the tests start (workers, smoke loads) import the same package and the test helpers.
os.environ["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(ROOT), *[p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]])
