"""Lessons, one flow (Alex, 2026-10-12): record -> show the proposed improvement -> apply after his go -> check whether
it works. Alex approves the exact edit; the runtime applies exactly that edit; a recurrence reopens the lesson."""
import sqlite3, sys, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "cato")]
from agent_core import learning
from agent_core.project_store import ProjectStore

SKILL = '---\nname: basket\nmetadata:\n  version: "1.8.5"\n---\n\n## Membership\n- Read the archives for each name.\n'


class Lessons(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        (self.root / "task_state").mkdir()
        con = sqlite3.connect(self.root / "task_state" / "conversation_recall.sqlite3")
        con.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, chat TEXT, speaker TEXT, text TEXT)")
        con.execute("INSERT INTO messages VALUES (7, '1', 'user', 'yes, change the skill like that')")
        con.execute("INSERT INTO messages VALUES (8, '1', 'assistant', 'yes, change the skill like that')")
        con.commit(); con.close()
        self.skill = self.root / "skills" / "basket" / "SKILL.md"
        self.skill.parent.mkdir(parents=True); self.skill.write_text(SKILL, encoding="utf-8")
        self.key = learning.propose(self.root, 1, "Freeze the membership before building the workbook", "t1", "basket_build")

    def tearDown(self):
        self.tmp.cleanup()

    def propose(self):
        return learning.propose_change(self.root, 1, self.key, self.skill, "- Read the archives for each name.",
                                       "- Freeze the membership first; then read the archives once per name.")

    def test_the_whole_flow_changes_the_skill_exactly_as_shown(self):
        shown = self.propose()["show_owner"]
        self.assertIn("Freeze the membership first", shown)
        self.assertNotIn("Freeze", self.skill.read_text(encoding="utf-8"))          # nothing changes before his go
        self.assertIn("edit proposed", learning.hints(self.root, 1, "basket_build"))
        self.assertEqual(learning.approve(self.root, 1, self.key, 7, "yes, change the skill like that", skip_trial=True)["status"], "applied")
        text = self.skill.read_text(encoding="utf-8")
        self.assertIn("- Freeze the membership first; then read the archives once per name.", text)
        self.assertIn('version: "1.8.6"', text)
        self.assertIn("APPLIED", learning.hints(self.root, 1, "basket_build"))       # checked on the next task
        self.assertEqual(learning.record_check(self.root, 1, self.key, True, "task 160: membership frozen first")["status"], "verified")
        self.assertNotIn(self.key, learning.hints(self.root, 1, "basket_build"))

    def test_approval_needs_a_shown_proposal_and_owners_own_words(self):
        with self.assertRaises(ValueError):
            learning.approve(self.root, 1, self.key, 7, "yes, change the skill like that")   # nothing shown yet
        self.propose()
        with self.assertRaises(ValueError):
            learning.approve(self.root, 1, self.key, 8, "yes, change the skill like that")   # assistant message
        with self.assertRaises(ValueError):
            learning.approve(self.root, 1, self.key, 7, "words he never said")

    def test_a_skill_edited_after_the_proposal_needs_a_new_proposal(self):
        self.propose()
        self.skill.write_text(SKILL + "- Another rule.\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            learning.approve(self.root, 1, self.key, 7, "yes, change the skill like that")

    def test_a_recurrence_reopens_the_lesson(self):
        self.propose(); learning.approve(self.root, 1, self.key, 7, "yes, change the skill like that", skip_trial=True)
        self.assertEqual(learning.record_check(self.root, 1, self.key, False, "task 161: rebuilt membership twice")["status"], "candidate")
        self.assertIn("candidate, unapproved", learning.hints(self.root, 1, "basket_build"))

    def test_an_added_rule_is_one_ledger_entry_counted_by_checks_and_retired_on_his_words(self):
        """ACE-style ledger (2026-10-24): an addition is an entry, never a rewrite; checks count helped/hurt; a verified
        entry keeps being counted and the digest offers it for retirement once it hurts more than it helps."""
        learning.propose_change(self.root, 1, self.key, self.skill, "", "Freeze the membership before building.")
        learning.approve(self.root, 1, self.key, 7, "yes, change the skill like that", skip_trial=True)
        text = self.skill.read_text(encoding="utf-8")
        self.assertIn("- Read the archives for each name.", text)                       # the prose is untouched
        self.assertIn(learning.LEDGER, text)
        self.assertIn(f"- [{self.key}] (", text)
        self.assertTrue(text.rstrip().endswith("Freeze the membership before building."))
        out = learning.record_check(self.root, 1, self.key, True, "task 160: frozen first")
        self.assertEqual((out["status"], out["helped"], out["hurt"]), ("verified", 1, 0))
        out = learning.record_check(self.root, 1, self.key, False, "task 161: rebuilt twice")   # verified stays, counted
        self.assertEqual((out["status"], out["helped"], out["hurt"]), ("verified", 1, 1))
        self.assertNotIn("RETIRE?", learning.digest(self.root, 1))
        learning.record_check(self.root, 1, self.key, False, "task 162: rebuilt again")
        self.assertIn(f"RETIRE? {self.key}: hurt 2, helped 1", learning.digest(self.root, 1))
        with self.assertRaises(ValueError):
            learning.retire(self.root, 1, self.key, 8, "yes, change the skill like that")   # assistant's words
        out = learning.retire(self.root, 1, self.key, 7, "yes, change the skill like that")
        self.assertEqual((out["status"], out["entry_removed"]), ("retired", True))
        text = self.skill.read_text(encoding="utf-8")
        self.assertNotIn(self.key, text)
        self.assertIn("- Read the archives for each name.", text)
        self.assertIn('version: "1.8.7"', text)
        self.assertNotIn(self.key, learning.digest(self.root, 1))

    def test_two_entries_saying_the_same_thing_are_offered_for_a_merge(self):
        learning.propose_change(self.root, 1, self.key, self.skill, "", "Freeze the membership list before the build.")
        learning.approve(self.root, 1, self.key, 7, "yes, change the skill like that", skip_trial=True)
        k2 = learning.propose(self.root, 1, "Freeze the membership list before building", "t2", "basket_build")
        learning.propose_change(self.root, 1, k2, self.skill, "", "Freeze the membership list before building.")
        learning.approve(self.root, 1, k2, 7, "yes, change the skill like that", skip_trial=True)
        self.assertIn(f"MERGE? {self.key} and {k2}", learning.digest(self.root, 1))
        self.assertEqual(self.skill.read_text(encoding="utf-8").count(learning.LEDGER), 1)

    def test_the_model_cannot_move_a_lesson_past_candidate(self):
        store = ProjectStore(self.root); row = store.manage(1, "read", "lesson", self.key)
        for status in ("proposed", "applied", "verified", "approved", "deployed"):
            with self.assertRaises(ValueError):
                store.manage(1, "save", "lesson", self.key, dict(row["data"], status=status), row["version"])

    def test_repeats_are_flagged(self):
        self.assertEqual(learning.similar(self.root, 1, "freeze membership before the workbook build"), self.key)
        self.assertEqual(learning.similar(self.root, 1, "Yahoo's TOPIX ETF has bad prints in March"), "")
        import background_runtime                       # the note a repeat triggers points at the one flow, changes nothing itself
        self.assertIn("lesson action=propose", background_runtime.REPEAT_NOTE)
        self.assertIn("stays a candidate", background_runtime.REPEAT_NOTE)


if __name__ == "__main__":
    unittest.main()
