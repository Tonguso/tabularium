"""Lessons That Change Methods (docs/design, 2026-11-10), step 1: a proposal names its cause and the observations it
explains; checks are measured or judgements; "apply it" without a usable case applies nothing; the ledger is capped;
fold absorbs proven entries into prose."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tabularium.learning import learning

SKILL = ('---\nname: basket\nmetadata:\n  version: "2.0.0"\n---\n\n## Membership\n- Read the archives for each name.\n'
         '- Build the workbook from the archives.\n')
ANCHORED = {"claim": "The method reads the archives before the membership is fixed, so a late change forces a rebuild.",
            "anchor": "- Read the archives for each name."}
SILENT = {"claim": "The method says nothing about when the membership is fixed.", "silent_at": "the Membership section"}
YES = (7, "yes, change the skill like that")


class Step1(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        (self.root / "task_state").mkdir()
        con = sqlite3.connect(self.root / "task_state" / "conversation_recall.sqlite3")
        con.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, chat TEXT, speaker TEXT, text TEXT)")
        con.execute("INSERT INTO messages VALUES (7, '1', 'user', 'yes, change the skill like that')")
        con.execute("INSERT INTO messages VALUES (8, '1', 'assistant', 'no')")
        con.commit(); con.close()
        self.skill = self.root / "skills" / "basket" / "SKILL.md"
        self.skill.parent.mkdir(parents=True); self.skill.write_text(SKILL, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def observe(self, text, source="message:7"):
        return learning.propose(self.root, 1, text, source, "basket_build")

    def task(self, n, result=True):
        folder = self.root / "task_state" / "background" / str(n)
        folder.mkdir(parents=True, exist_ok=True)
        if result: (folder / "result.txt").write_text("done", encoding="utf-8")
        return folder

    def measured(self, n):
        self.task(n)
        return dict(task=n, receipt=f"task:{n}", method_sha=learning._sha(self.skill))

    def applied_entry(self, text, checks=0):
        """A ledger entry applied on the owner's go, with `checks` measured checks."""
        key = self.observe(text)
        learning.propose_change(self.root, 1, key, self.skill, "", text, SILENT)
        learning.approve(self.root, 1, key, *YES, skip_trial=True)
        for n in range(checks):
            learning.record_check(self.root, 1, key, True, f"task {900 + n}", **self.measured(900 + n))
        return key

    # 4.1 a lesson names its cause
    def test_a_proposal_without_a_cause_or_with_an_anchor_not_in_the_method_is_refused(self):
        key = self.observe("Rebuilt the workbook after a late membership change")
        with self.assertRaisesRegex(ValueError, "names its cause"):
            learning.propose_change(self.root, 1, key, self.skill, "- Read the archives for each name.", "- Freeze first.")
        with self.assertRaisesRegex(ValueError, "quoted exactly from the method"):
            learning.propose_change(self.root, 1, key, self.skill, "- Read the archives for each name.", "- Freeze first.",
                                    {"claim": "x", "anchor": "- Not in the method."})
        with self.assertRaisesRegex(ValueError, "rewrites the rule the cause anchors"):   # the edit must touch the anchored rule
            learning.propose_change(self.root, 1, key, self.skill, "- Build the workbook from the archives.", "- Build it last.", ANCHORED)
        with self.assertRaisesRegex(ValueError, "silent_at"):                               # an append needs a silence
            learning.propose_change(self.root, 1, key, self.skill, "", "- Freeze first.", {"claim": "x"})
        self.assertEqual(learning.read(self.root, 1, key)["status"], "candidate")            # the observation stays
        shown = learning.propose_change(self.root, 1, key, self.skill, "- Read the archives for each name.",
                                        "- Freeze the membership; then read the archives once per name.", ANCHORED)
        self.assertIn("CAUSE: The method reads the archives", shown["show_owner"])
        self.assertIn("ANCHOR: - Read the archives for each name.", shown["show_owner"])
        d = learning.read(self.root, 1, key)
        self.assertEqual(d["cause"]["anchor"], ANCHORED["anchor"])
        self.assertEqual(d["cause"]["file_sha256"], learning._sha(self.skill))
        self.assertEqual(d["explains"], [key])

    def test_one_instance_needs_a_receipt_and_explained_observations_must_be_open_and_of_the_same_method(self):
        lone = self.observe("Rebuilt the workbook after a late change", source="a sentence, no record")
        with self.assertRaisesRegex(ValueError, "observation, not a cause"):
            learning.propose_change(self.root, 1, lone, self.skill, "", "- Freeze first.", SILENT)
        self.assertEqual(learning.read(self.root, 1, lone)["status"], "candidate")
        second = self.observe("Rebuilt again after the membership moved", source="still no record")
        shown = learning.propose_change(self.root, 1, lone, self.skill, "", "- Freeze first.", SILENT, explains=[second])
        self.assertEqual(learning.read(self.root, 1, lone)["explains"], [lone, second])     # recurrence is evidence enough
        self.assertIn("EXPLAINS: " + lone + ", " + second, shown["show_owner"])
        with_receipt = self.observe("Rebuilt the workbook, task 41", source=str(self.task(41) / "checkpoint.json"))
        learning.propose_change(self.root, 1, with_receipt, self.skill, "", "- Freeze first, really.", SILENT)
        other_method = self.root / "SKILL.md"; other_method.write_text("# Persona\n", encoding="utf-8")
        persona = self.observe("Asked twice")
        learning.propose_change(self.root, 1, persona, other_method, "", "Ask once.", {"claim": "c", "silent_at": "asking"})
        third = self.observe("Rebuilt a third time")
        with self.assertRaisesRegex(ValueError, "is about SKILL.md, not SKILL.md|not an open observation|is about"):
            learning.propose_change(self.root, 1, third, self.skill, "", "- Freeze.", SILENT, explains=[persona])
        learning.retire(self.root, 1, persona, *YES)
        with self.assertRaisesRegex(ValueError, "not an open observation"):
            learning.propose_change(self.root, 1, third, self.skill, "", "- Freeze.", SILENT, explains=[persona])

    # 4.3 the ledger cap and fold
    def test_a_ledger_at_the_cap_refuses_an_addition_and_accepts_a_rewrite_a_removal_and_a_fold(self):
        keys = [self.applied_entry(f"Rule number {n}.", checks=1) for n in range(learning.LEDGER_CAP)]
        self.assertEqual(len(learning.ledger_entries(self.skill.read_text(encoding="utf-8"))), learning.LEDGER_CAP)
        extra = self.observe("One more reminder")
        with self.assertRaisesRegex(ValueError, "holds 5 entries"):
            learning.propose_change(self.root, 1, extra, self.skill, "", "Rule number six.", SILENT)
        rewrite = self.observe("The archive rule is the cause")
        learning.propose_change(self.root, 1, rewrite, self.skill, "- Read the archives for each name.",
                                "- Freeze the membership; then read the archives once per name.", ANCHORED)
        learning.approve(self.root, 1, rewrite, *YES, skip_trial=True)                    # a rewrite is always allowed
        self.assertIn("Freeze the membership; then read", self.skill.read_text(encoding="utf-8"))
        learning.retire(self.root, 1, keys[0], *YES)                                        # a removal is always allowed
        self.assertNotIn(keys[0], self.skill.read_text(encoding="utf-8"))
        unproven = self.applied_entry("An entry nobody measured.")
        with self.assertRaisesRegex(ValueError, "no measured check"):
            learning.propose_fold(self.root, 1, self.skill, "- Build the workbook from the archives.", "- Build it once.", [unproven])
        fold = learning.propose_fold(self.root, 1, self.skill, "- Build the workbook from the archives.",
                                     "- Build the workbook once, from the frozen membership (rules one to three).", keys[1:4])
        self.assertIn("REMOVES ENTRIES", fold["show_owner"])
        self.assertTrue(learning.proposals(self.root, 1)[0]["fold"])
        before = self.skill.read_text(encoding="utf-8")
        self.skill.write_text(before + "- Someone else edited.\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed since the fold was proposed"):   # one write guarded by the hash
            learning.approve(self.root, 1, fold["key"], *YES, skip_trial=True)
        self.skill.write_text(before, encoding="utf-8")
        learning.approve(self.root, 1, fold["key"], *YES, skip_trial=True)
        text = self.skill.read_text(encoding="utf-8")
        self.assertIn("Build the workbook once, from the frozen membership", text)
        for k in keys[1:4]:
            self.assertNotIn(k, text)
            self.assertEqual(learning.read(self.root, 1, k)["status"], "folded")           # keys stay as provenance
            self.assertEqual(learning.read(self.root, 1, k)["folded_into"], fold["key"])
            self.assertEqual(len(learning.read(self.root, 1, k)["checks"]), 1)
        self.assertEqual(len(learning.ledger_entries(text)), 2)                              # keys[4] and the unproven one

    # 4.5 the bypass is closed
    def test_apply_it_without_a_usable_case_applies_nothing_and_the_owner_chooses(self):
        key = self.observe("Rebuilt after a late change")
        learning.propose_change(self.root, 1, key, self.skill, "- Read the archives for each name.", "- Freeze first; then read.", ANCHORED)
        out = learning.settle_go(self.root, 1, key, "apply it", *YES, store=None)
        self.assertIn("not applied", out); self.assertIn("draft a case first", out)
        d = learning.read(self.root, 1, key)
        self.assertEqual(d["status"], "proposed"); self.assertNotIn("Freeze first", self.skill.read_text(encoding="utf-8"))
        self.assertIn("reason", d["go_blocked"]); self.assertEqual(d["go_blocked"]["quote"], YES[1])
        with self.assertRaises(ValueError):                                                   # not his words
            learning.settle_go(self.root, 1, key, "draft a case first", 8, "no", store=None)
        out = learning.settle_go(self.root, 1, key, "draft a case first", *YES, store=None)
        self.assertIn("a case first", out)
        d = learning.read(self.root, 1, key)
        self.assertEqual(d["case_requested"]["quote"], YES[1]); self.assertNotIn("go_blocked", d)
        self.assertEqual(learning.proposals(self.root, 1), [])                                 # off his list until the trial
        self.assertIn("wants a case before the trial", learning.hints(self.root, 1, "basket_build"))
        self.assertEqual(d["status"], "proposed")
        out = learning.settle_go(self.root, 1, key, "apply it, no trial needed", *YES, store=None)
        self.assertIn("no trial", out)
        self.assertEqual(learning.read(self.root, 1, key)["trial_skipped"]["reason"], "Alex: no trial needed")

    # 4.6 measured checks
    def test_a_check_without_task_receipt_and_method_version_is_a_judgement_that_counts_nothing(self):
        key = self.applied_entry("Freeze the membership before building.")
        out = learning.record_check(self.root, 1, key, True, "I think it helped")
        self.assertEqual((out["judgement"], out["helped"], out["hurt"], out["status"]), (True, 0, 0, "applied"))
        with self.assertRaisesRegex(ValueError, "together"):                                 # all three or none
            learning.record_check(self.root, 1, key, True, "x", task=12)
        with self.assertRaisesRegex(ValueError, "No task 12"):
            learning.record_check(self.root, 1, key, True, "x", task=12, receipt="task:12", method_sha="a" * 64)
        self.task(12, result=False)
        with self.assertRaisesRegex(ValueError, "no result"):
            learning.record_check(self.root, 1, key, True, "x", task=12, receipt="task:12", method_sha="a" * 64)
        with self.assertRaisesRegex(ValueError, "No owner message 8"):
            learning.record_check(self.root, 1, key, True, "x", task=12, receipt="message:8", method_sha="a" * 64)
        with self.assertRaisesRegex(ValueError, "64 hex"):
            learning.record_check(self.root, 1, key, True, "x", task=12, receipt="message:7", method_sha="short")
        out = learning.record_check(self.root, 1, key, True, "task 12: frozen first", task=12, receipt="message:7", method_sha=learning._sha(self.skill))
        self.assertEqual((out["status"], out["helped"]), ("verified", 1))
        with self.assertRaisesRegex(ValueError, "already counted"):                           # one task, one count
            learning.record_check(self.root, 1, key, False, "task 12 again", task=12, receipt="message:7", method_sha=learning._sha(self.skill))
        checks = learning.read(self.root, 1, key)["checks"]
        self.assertEqual([c.get("judgement", False) for c in checks], [True, False])
        self.assertEqual(checks[1]["receipt"], "message:7")


if __name__ == "__main__":
    unittest.main()
