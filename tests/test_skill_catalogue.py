"""Every installed skill must reach the catalogue with a description (2026-10-10: a byte-order mark on two SKILL.md
files made the parser return nothing, so the two most-used skills were listed with empty descriptions)."""
import sys, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "cato")]
import engine


class SkillCatalogue(unittest.TestCase):
    def test_every_skill_has_a_description(self):
        for agent in ("cato", "tacitus", "callimachus", "vitruvius"):
            runtime = engine.AgentRuntime()
            runtime.SKILLS_DIR = ROOT / agent / "skills"
            for name, skill in runtime.discover_skills().items():
                self.assertTrue(skill["description"].strip(), f"{agent}: skill {name} has no description")

    def test_no_byte_order_marks(self):
        bad = [p for p in ROOT.glob("*/skills/*/SKILL.md") if p.read_bytes().startswith(b"\xef\xbb\xbf")]
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
