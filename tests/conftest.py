"""Test configuration: the package under test is the one in src/, the home folder is the repository.

The tests import `tabularium` from src/ whether or not the package is installed, and they never pick up another
checkout that an environment file (a .pth) may have put on the path: any path entry that carries a flat-layout
`engine.py` or an `agent_core` package is removed first.
"""
import tempfile
from pathlib import Path

# Temp folders are canonical paths. The runtime resolves every path it is given (its home, a file to write), so a test
# that builds its expectation from the raw temp path must see the same spelling. GitHub's Windows runner hands out an
# 8.3 alias (C:\Users\RUNNER~1\...) and three tests compared it with the resolved form (2026-11-11).
tempfile.tempdir = str(Path(tempfile.gettempdir()).resolve())
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:] = [p for p in sys.path if not ((Path(p or ".") / "agent_core").is_dir() or (Path(p or ".") / "engine.py").is_file())]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("TABULARIUM_HOME", str(ROOT))
# Child interpreters the tests start (workers, smoke loads) import the same package and the test helpers.
os.environ["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(ROOT), *[p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]])
