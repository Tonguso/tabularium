"""A tool's refusal or failure is in its type, never guessed from its first words (2026-10-15).

Why: engine.unproductive matched a result's opening against ten strings to feed the runaway stop. A refusal phrased
differently passed as success. Now refusals are Refused(...) and failures Failed(...) (tabularium.core.outcome), the stop
reads the type, and this test fails on any handler that returns a refusal or error as a bare string."""
import ast, re, unittest
from pathlib import Path
from tabularium.core.outcome import Refused, Failed
ROOT = Path(__file__).resolve().parents[1]
FILES = sorted((ROOT / "src" / "tabularium").rglob("*.py"))
MARKERS = re.compile(r"^(Error\b|Refused\b|NOT executed|Needs foreground approval|Command cancelled|Command timed out|"
                     r"Quality review timed out|Stopped at the next tool boundary|Tool \S+ is not available)")


def opening(node):
    """The literal text a return value starts with, when it is a string literal or f-string; else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values and isinstance(node.values[0], ast.Constant):
        return node.values[0].value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return opening(node.left)
    return None


class Outcomes(unittest.TestCase):
    def test_types_are_strings_on_the_wire(self):
        r = Refused("no"); f = Failed("Error: x")
        self.assertEqual((r, f, isinstance(r, str), r == "no", str(f)), ("no", "Error: x", True, True, "Error: x"))

    def test_no_handler_returns_a_refusal_or_error_as_a_bare_string(self):
        bare = []
        for path in FILES:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
                if isinstance(node, ast.Return) and node.value is not None:
                    text = opening(node.value)
                    if text is not None and MARKERS.match(text.lstrip()):
                        bare.append(f"{path.name}:{node.lineno}: {text[:50]!r}")
        self.assertEqual(bare, [])


if __name__ == "__main__":
    unittest.main()
