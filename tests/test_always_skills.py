"""ALWAYS_SKILLS (2026-10-09): a skill named there is standing instruction, included in full in the system prompt;
the others stay a catalogue. Tacitus's standing method is the first such skill."""
import sys, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
from tabularium.core import engine
def block_for(skills_dir, always):
    runtime = engine.AgentRuntime()
    runtime.SKILLS_DIR = Path(skills_dir)
    runtime.ALWAYS_SKILLS = always
    return runtime.skills_prompt_block()



class AlwaysSkills(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); d = Path(self.tmp.name)
        for name, body in (("method", "DESCRIBE, REVIEW, LINK."), ("other", "How to make a deck.")):
            (d / name).mkdir()
            (d / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name} description\n---\n\n{body}\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_standing_skill_is_inlined_and_the_rest_catalogued(self):
        text = block_for(self.tmp.name, ("method",))
        self.assertIn("## Standing method: method", text)
        self.assertIn("DESCRIBE, REVIEW, LINK.", text)
        self.assertIn("other", text)
        self.assertNotIn("other description", text)
        self.assertNotIn("- **method** —", text)
        self.assertNotIn("How to make a deck.", text)

    def test_without_standing_skills_descriptions_are_retrievable(self):
        text = block_for(self.tmp.name, ())
        self.assertNotIn("Standing method", text)
        self.assertIn("method", text)
        runtime = engine.AgentRuntime()
        runtime.SKILLS_DIR = Path(self.tmp.name)
        self.assertIn("method description", runtime.tool_skill(""))
        self.assertIn("other description", runtime.tool_skill(""))

    def test_tacitus_declares_the_personal_method(self):
        src = (ROOT / "src" / "tabularium" / "agents" / "private.py").read_text(encoding="utf-8")
        self.assertIn('always_skills=("standing-method",)', src)
        self.assertTrue((ROOT / "tacitus" / "skills" / "standing-method" / "SKILL.md").exists())


if __name__ == "__main__":
    unittest.main()
