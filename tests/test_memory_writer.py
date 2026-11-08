"""remember() is the one memory writer, and it routes by kind (2026-10-08). Tested on the real agent with a real
recall store, so a sourced fact goes through the actual provenance check, not a stand-in."""
import sys, tempfile, unittest
from contextlib import closing
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from agent_harness import make_agent
from agent_core.project_store import ProjectStore


class Remember(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "cato"; self.root.mkdir()
        self.agent = make_agent("cato", self.root)
        self.agent.MEMORY_FILE.write_text("# M\n\n## Notes\n- old wording here\n", encoding="utf-8")
        self.agent._recall.record(1, [{"role": "user", "content": "[2026-10-14 Tuesday 10:00] Keep the basket to six names."}],
                                  str(self.root / "conversation_1.json"))
        with closing(self.agent._recall.connect()) as db:
            self.message_id = db.execute("SELECT id FROM messages WHERE speaker='user'").fetchone()[0]

    def tearDown(self):
        self.tmp.cleanup()

    def memory(self):
        return self.agent.MEMORY_FILE.read_text(encoding="utf-8")

    def test_a_sourced_fact_needs_owners_exact_words(self):
        remember = self.agent.tool_remember
        self.assertIn("Fact not saved", remember(kind="user_decision", content="Basket of six", source=f"message:{self.message_id}",
                                                 quote="words he never said", chat_id=1))
        out = remember(kind="user_decision", content="Basket of six", source=f"message:{self.message_id}", quote="six names", chat_id=1)
        self.assertNotIn("not saved", out)
        self.assertIn("Basket of six", self.memory())

    def test_note_correction_thread_and_lesson(self):
        remember = self.agent.tool_remember
        remember(kind="note", content="Alex likes short replies", chat_id=1)
        self.assertIn("(unsourced note): Alex likes short replies", self.memory())
        remember(old="old wording here", new="new wording here", chat_id=1)
        self.assertIn("new wording here", self.memory())
        self.assertIn("needs both", remember(old="only old", chat_id=1))
        self.assertIn("team-speech", remember(kind="thread", name="team-speech", state="slides 21-39 to script", chat_id=1))
        # 2026-11-01: a lesson is recorded only together with its skill edit, and is then already proposed to Alex
        self.assertIn("Without the edit nothing is recorded", remember(kind="lesson", content="test the fetch script on two names first", chat_id=1))
        (self.root / "SKILL.md").write_text("# persona" + chr(10), encoding="utf-8")
        self.assertIn("registered for Alex's go", remember(kind="lesson", content="test the fetch script on two names first",
                                                            method_path=str(self.root / "SKILL.md"), new="Test the fetch script on two names first.", chat_id=1))
        self.assertEqual(ProjectStore(self.root).manage(1, "list", "lesson")[0]["status"], "proposed")

    def test_unknown_kind_explains_itself(self):
        self.assertIn("needs kind", self.agent.tool_remember(content="x", chat_id=1))


if __name__ == "__main__":
    unittest.main()
