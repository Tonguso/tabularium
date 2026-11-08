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

    def test_a_lesson_is_recorded_only_with_its_edit_and_is_then_already_proposed(self):
        # 2026-11-01: 183 candidates and 40 proposals nobody showed Alex; "those lessons never amounted to anything".
        shown = learning.record(self.root, 1, "Freeze the membership list before building", "t2", self.skill,
                                "- Read the archives for each name.", "- Freeze the membership first.")
        self.assertEqual(learning.read(self.root, 1, shown["key"])["status"], "proposed")
        self.assertIn("REPLACE:", shown["show_owner"])
        before = len(learning._lessons(self.root, 1, learning.OPEN))
        with self.assertRaises(ValueError):
            learning.record(self.root, 1, "A lesson with a misquoted edit", "t3", self.skill, "- Not in the file.", "- New rule.")
        self.assertEqual(len(learning._lessons(self.root, 1, learning.OPEN)), before)      # nothing recorded without a valid edit

    def test_a_habit_of_conversation_edits_the_persona_file(self):
        persona = self.root / "SKILL.md"; persona.write_text("# Cato\n\nAnswer in his register.\n", encoding="utf-8")
        shown = learning.record(self.root, 1, "Asked Alex to confirm a plain instruction twice", "t4", persona, "",
                                "Act on a plain instruction; ask only when two readings differ.")
        self.assertIn("ADD AT THE END", shown["show_owner"])
        self.assertEqual(learning.proposals(self.root, 1)[0]["skill"], "SKILL.md")

    def test_proposals_are_listed_for_the_dashboard_and_the_weekly_message(self):
        self.assertEqual(learning.proposals(self.root, 1), [])
        self.assertEqual(learning.awaiting_go(self.root, 1), "")
        self.propose()
        self.assertEqual([r["skill"] for r in learning.proposals(self.root, 1)], ["basket/SKILL.md"])
        text = learning.awaiting_go(self.root, 1)
        self.assertIn("1 skill edit(s) await your go", text)
        self.assertIn("Freeze the membership first", text)

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
        self.skill.write_text(SKILL.replace("- Read the archives for each name.", "- Read the filings for each name."), encoding="utf-8")
        with self.assertRaises(ValueError):                                   # the passage he saw replaced is gone
            learning.approve(self.root, 1, self.key, 7, "yes, change the skill like that", skip_trial=True)
        self.skill.write_text(SKILL + "- Another rule.\n", encoding="utf-8")   # another edit to the same file is no obstacle
        self.assertEqual(learning.approve(self.root, 1, self.key, 7, "yes, change the skill like that", skip_trial=True)["status"], "applied")

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

    def test_a_proposed_edit_is_dropped_on_his_words_and_leaves_the_list(self):
        """2026-11-02: the dashboard's Drop button. Nothing in the skill changes; the proposal stops waiting on him."""
        self.propose()
        self.assertEqual(len(learning.proposals(self.root, 1)), 1)
        out = learning.retire(self.root, 1, self.key, 7, "yes, change the skill like that")
        self.assertEqual((out["status"], out["entry_removed"]), ("retired", False))
        self.assertIn("Dropped before it was applied", out["note"])
        self.assertEqual(learning.proposals(self.root, 1), [])
        self.assertEqual(self.skill.read_text(encoding="utf-8"), SKILL)

    def test_his_dashboard_verdict_is_settled_by_the_runtime_not_by_the_agent(self):
        """2026-11-08: 25 edits he had said "apply it" to stood on his list for a day. drop retires; apply applies at
        once when the skill has no benchmark cases to trial on, and the record says so."""
        self.propose()
        out = learning.settle_go(self.root, 1, self.key, "apply it", 7, "yes, change the skill like that", store=None)
        self.assertIn("applied to SKILL.md; no trial possible", out)
        d = learning.read(self.root, 1, self.key)
        self.assertEqual(d["status"], "applied")
        self.assertIn("no trial possible", d["trial_skipped"]["reason"])
        self.assertIn("Freeze the membership first", self.skill.read_text(encoding="utf-8"))
        self.assertEqual(learning.proposals(self.root, 1), [])
        key2 = learning.propose(self.root, 1, "Another lesson", "t2", "basket_build")
        learning.propose_change(self.root, 1, key2, self.skill, "", "- Say when the archives were read.")
        self.assertIn("Dropped before it was applied", learning.settle_go(self.root, 1, key2, "drop it", 7, "yes, change the skill like that", store=None))
        self.assertEqual(learning.read(self.root, 1, key2)["status"], "retired")
        with self.assertRaises(ValueError):                                   # settled twice changes nothing
            learning.settle_go(self.root, 1, key2, "drop it", 7, "yes, change the skill like that", store=None)

    def test_an_edit_on_trial_is_the_runtimes_work_and_returns_with_the_report(self):
        self.propose()
        learning.set_trial(self.root, 1, self.key, {"version": 2, "notified": False})
        self.assertEqual(learning.proposals(self.root, 1), [])
        report = self.root / "report.md"; report.write_text("variant 3/4, baseline 1/4", encoding="utf-8")
        learning.set_trial(self.root, 1, self.key, {"version": 2, "notified": True, "report": str(report)})
        self.assertIn("TRIAL DONE:\nvariant 3/4", learning.proposals(self.root, 1)[0]["shown"])

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


class RememberRecordsTheEditOrNothing(unittest.TestCase):
    def test_remember_kind_lesson_needs_the_edit_and_registers_a_proposal(self):
        import contextlib, io
        from agent_harness import make_agent
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "cato"; root.mkdir()
            with contextlib.redirect_stdout(io.StringIO()):
                agent = make_agent("cato", root)
            (root / "SKILL.md").write_text("# Cato\n", encoding="utf-8")
            out = agent.execute_tool("remember", {"kind": "lesson", "content": "I asked who the buyer was"}, 1)
            self.assertIn("Without the edit nothing is recorded", str(out))
            self.assertEqual(learning._lessons(root, 1, learning.OPEN), [])
            out = agent.execute_tool("remember", {"kind": "lesson", "content": "I asked who the buyer was",
                                                  "method_path": str(root / "SKILL.md"), "new": "Act on a plain instruction."}, 1)
            self.assertIn("registered for Alex's go", str(out))
            self.assertEqual(learning.proposals(root, 1)[0]["skill"], "SKILL.md")


if __name__ == "__main__":
    unittest.main()
