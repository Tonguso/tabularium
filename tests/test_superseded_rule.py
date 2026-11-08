"""The superseded-facts archive is defined once (2026-10-15).

Why: five sites in three modules each carried their own regex for the "## Superseded facts" section, in two spellings.
memory_store.current_facts and superseded_section are now the only definition."""
import re, unittest
from pathlib import Path
from tabularium.memory.memory_store import current_facts, superseded_section
ROOT = Path(__file__).resolve().parents[1]

TEXT = "# Memory\n\n## Facts\n- a\n\n## Superseded facts (historical, not current)\n- old a\n\n## Later\n- b\n"


class SupersededRule(unittest.TestCase):
    def test_current_facts_drops_only_the_archive(self):
        self.assertEqual(current_facts(TEXT), "# Memory\n\n## Facts\n- a\n\n## Later\n- b\n")
        self.assertEqual(superseded_section(TEXT).group().splitlines()[0], "## Superseded facts (historical, not current)")
        self.assertIsNone(superseded_section("## Facts\n- a\n"))
        self.assertEqual(current_facts("## Superseded facts\n- x\n"), "")          # the short heading spelling too

    def test_no_other_module_defines_the_rule(self):
        hits = [p.name for p in (ROOT / "src" / "tabularium").rglob("*.py") if p.name != "memory_store.py"
                and re.search(r"Superseded facts.*\\n", p.read_text(encoding="utf-8-sig"))]
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
