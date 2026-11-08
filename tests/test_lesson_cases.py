"""Every lesson carries its own benchmark case (2026-10-26, Alex: [quote removed]).

A correction's lesson drafts a case from its task; the agent fills the oracle and checkable points; calibration (floor:
the current skill alone, ceiling: the skill with the rule stated) refuses broken and non-discriminating cases by code;
the owner's quoted yes adds the rest to the benchmark; approved cases of one skill recombine into a reserved case
drafted by code; a trial with no chosen cases runs on the skill's own cases."""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from agent_harness import put
from tabularium.learning import learning
from tabularium.learning import lesson_cases
from tabularium.learning import regression
from tabularium.learning import trials
from tabularium.records import background_runtime as rt
SKILL = '---\nname: basket\nmetadata:\n  version: "1.8.5"\n---\n\nRead archives.\n'


def requirement(value, ident="value"):
    return dict(id=ident, description=f"result.txt holds {value}", check=dict(kind="text", path="result.txt", expected=value))


class LessonCases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "cato"
        (self.root / "task_state").mkdir(parents=True)
        db = sqlite3.connect(self.root / "task_state/conversation_recall.sqlite3")
        try:
            db.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, chat TEXT, speaker TEXT, text TEXT)")
            db.execute("INSERT INTO messages VALUES (7, '1', 'user', 'yes, add that case')")
            db.execute("INSERT INTO messages VALUES (8, '1', 'assistant', 'yes, add that case')")
            db.commit()
        finally:
            db.close()
        self.skill = self.root / "skills/basket/SKILL.md"
        self.skill.parent.mkdir(parents=True)
        self.skill.write_text(SKILL, encoding="utf-8")
        self.store = rt.TaskStore(self.root)
        env = patch.dict(os.environ)
        env.start(); self.addCleanup(env.stop)
        for name in (trials.READS_ENV, trials.TRIAL_DIR_ENV, trials.OVERRIDES_ENV):
            os.environ.pop(name, None)

    def lesson(self, rule="Save the value the owner named, nothing else", request="Save result.txt containing A."):
        task = self.store.start(1, request, "")["id"]
        key = learning.propose(self.root, 1, rule, str(self.store.directory(task) / "checkpoint.json"), "basket_build")
        learning.propose_change(self.root, 1, key, self.skill, "", rule + ".")
        return key

    def read(self, key, slot="case"):
        return learning.read(self.root, 1, key)[slot]

    def settle(self, key, slot, floor, ceiling):
        for run in self.read(key, slot)["calibration"]["runs"]:
            folder = self.store.directory(run["task"])
            (folder / "trial_output").mkdir(exist_ok=True)
            (folder / "trial_output/result.txt").write_text(floor if run["arm"] == "floor" else ceiling, encoding="utf-8")
            put(self.store, run["task"], "finished")
        lesson_cases.reconcile(self.store)
        return self.read(key, slot)

    def calibrated(self, key, slot="case"):
        lesson_cases.calibrate(self.root, 1, key, self.store, "You are the agent.", slot)
        self.assertEqual(self.settle(key, slot, "X", "A")["status"], "calibrated")
        return lesson_cases.approve(self.root, 1, key, 7, "yes, add that case", slot)

    def test_a_lesson_drafts_its_case_from_the_tasks_request_and_the_owners_rule(self):
        key = self.lesson()
        case = self.read(key)
        self.assertEqual(case["status"], "draft")
        self.assertEqual(case["request"], "Save result.txt containing A.")
        self.assertIn("nothing else", case["rule"])
        self.assertEqual(case["requirements"], [])
        self.assertIn("still need an oracle", learning.digest(self.root, 1))

    def test_the_agent_fills_the_oracle_and_points_in_benchmark_form(self):
        key = self.lesson()
        with self.assertRaises(ValueError):                              # not a benchmark check
            lesson_cases.fill(self.root, 1, key, "case", None, "Owner named A", [dict(id="v", check=dict(kind="vibes"))])
        out = lesson_cases.fill(self.root, 1, key, "case", None, "Owner named A", [requirement("A")])
        self.assertEqual((out["status"], out["requirements"]), ("draft", 1))
        regression.validate_case(lesson_cases.record(key, learning.read(self.root, 1, key)))
        self.assertNotIn("still need an oracle", learning.digest(self.root, 1))

    def test_calibration_runs_both_arms_in_isolation_with_the_skill_as_it_stands(self):
        key = self.lesson()
        lesson_cases.fill(self.root, 1, key, "case", None, "Owner named A", [requirement("A")])
        out = lesson_cases.calibrate(self.root, 1, key, self.store, "You are the agent.")
        self.assertEqual(len(out["tasks"]), 2 * lesson_cases.RUNS)
        arms = {}
        for ident in out["tasks"]:
            ctx = json.loads((self.store.directory(ident) / "context.json").read_text(encoding="utf-8"))
            self.assertIs(ctx["notify_telegram"], False)
            self.assertEqual(ctx["trial"]["calibration"], "case")
            arms[ctx["trial"]["arm"]] = self.store.get(ident)["request"]
        self.assertNotIn("Rule to apply", arms["floor"])
        self.assertIn("Rule to apply, stated by the owner: Save the value", arms["ceiling"])
        self.assertEqual(Path(json.loads((self.store.directory(out["tasks"][0]) / "context.json").read_text(encoding="utf-8"))["trial"]["skill"]).read_text(encoding="utf-8"), SKILL)
        with self.assertRaises(ValueError):
            lesson_cases.calibrate(self.root, 1, key, self.store, "You are the agent.")     # already running

    def test_calibration_refuses_broken_and_non_discriminating_cases_and_keeps_the_rest(self):
        for floor, ceiling, status, reason in (("A", "A", "rejected", "not discriminating"),
                                               ("X", "X", "rejected", "broken"),
                                               ("X", "A", "calibrated", "makes the difference")):
            key = self.lesson(rule=f"Rule for {floor}{ceiling}")
            lesson_cases.fill(self.root, 1, key, "case", None, "Owner named A", [requirement("A")])
            lesson_cases.calibrate(self.root, 1, key, self.store, "You are the agent.")
            case = self.settle(key, "case", floor, ceiling)
            self.assertEqual(case["status"], status, (floor, ceiling))
            self.assertIn(reason, case["calibration"]["verdict"])
            self.assertTrue((Path(case["calibration"]["folder"]) / "evidence.json").exists())
        digest = learning.digest(self.root, 1)
        self.assertIn("CASE? ", digest)
        self.assertIn("refused: broken", digest)

    def test_calibration_is_a_band_over_repeated_runs_not_one_flaky_pair(self):
        """2026-11-02 (GDPevo): one pass without the rule, or one failure with it, no longer decides the case."""
        def settle_runs(key, floor_outputs, ceiling_outputs):
            runs = self.read(key)["calibration"]["runs"]
            outputs = {"floor": list(floor_outputs), "ceiling": list(ceiling_outputs)}
            for run in runs:
                folder = self.store.directory(run["task"]); (folder / "trial_output").mkdir(exist_ok=True)
                (folder / "trial_output/result.txt").write_text(outputs[run["arm"]].pop(0), encoding="utf-8")
                put(self.store, run["task"], "finished")
            lesson_cases.reconcile(self.store)
            return self.read(key)
        for floor, ceiling, status, reason in ((["X", "A", "X"], ["A", "A", "A"], "calibrated", "1 of 3 passed without it, 3 of 3 with it"),
                                               (["A", "A", "X"], ["A", "A", "A"], "rejected", "already passes 2 of 3"),
                                               (["X", "X", "X"], ["A", "X", "X"], "rejected", "only 1 of 3 runs passed")):
            key = self.lesson(rule=f"Rule for {''.join(floor)}{''.join(ceiling)}")
            lesson_cases.fill(self.root, 1, key, "case", None, "Owner named A", [requirement("A")])
            lesson_cases.calibrate(self.root, 1, key, self.store, "You are the agent.")
            case = settle_runs(key, floor, ceiling)
            self.assertEqual(case["status"], status, (floor, ceiling))
            self.assertIn(reason, case["calibration"]["verdict"])

    def test_a_foreground_lesson_takes_its_request_from_owners_message_and_its_rule_from_the_edit(self):
        """2026-11-02: a lesson from a conversation had an empty request, and its rule was the failure sentence."""
        db = sqlite3.connect(self.root / "task_state/conversation_recall.sqlite3")
        try:
            db.execute("INSERT INTO messages VALUES (9, '1', 'user', '[2026-11-02 Monday 09:00] Build me the Q4 basket workbook')")
            db.commit()
        finally:
            db.close()
        key = learning.record(self.root, 1, "I built the workbook before the membership was frozen", "message:9",
                              self.skill, "", "Freeze the membership before building the workbook.")["key"]
        case = self.read(key)
        self.assertEqual(case["request"], "Build me the Q4 basket workbook")
        self.assertEqual(case["rule"], "Freeze the membership before building the workbook.")

    def test_a_crashed_arm_is_a_failed_run_and_the_case_is_refused_not_retried(self):
        key = self.lesson()
        lesson_cases.fill(self.root, 1, key, "case", None, "Owner named A", [requirement("A")])
        lesson_cases.calibrate(self.root, 1, key, self.store, "You are the agent.")
        runs = self.read(key)["calibration"]["runs"]
        for run in runs:
            folder = self.store.directory(run["task"]); (folder / "trial_output").mkdir(exist_ok=True)
            (folder / "trial_output/result.txt").write_text("A", encoding="utf-8")
            put(self.store, run["task"], "interrupted" if run["arm"] == "ceiling" else "finished", "Worker error: test")
        lesson_cases.reconcile(self.store)
        self.assertEqual(self.read(key)["status"], "rejected")
        self.assertIn("broken", self.read(key)["calibration"]["verdict"])

    def test_the_owners_words_add_a_calibrated_case_to_the_benchmark_frozen(self):
        key = self.lesson()
        with self.assertRaises(ValueError):
            lesson_cases.approve(self.root, 1, key, 7, "yes, add that case")               # not calibrated
        lesson_cases.fill(self.root, 1, key, "case", None, "Owner named A", [requirement("A")])
        lesson_cases.calibrate(self.root, 1, key, self.store, "You are the agent.")
        self.settle(key, "case", "X", "A")
        with self.assertRaises(ValueError):
            lesson_cases.approve(self.root, 1, key, 8, "yes, add that case")               # the assistant's words
        with self.assertRaises(ValueError):
            lesson_cases.approve(self.root, 1, key, 7, "words he never said")
        out = lesson_cases.approve(self.root, 1, key, 7, "yes, add that case")
        self.assertEqual((out["status"], out["benchmark_cases"]), ("approved", 1))
        ids = [c["id"] for c in regression.load_cases(self.root)]
        self.assertIn(key + "-case", ids)
        self.assertIn("invoice-v1", ids)                                                  # the shared benchmark stays
        case = next(c for c in regression.load_cases(self.root) if c["id"] == key + "-case")
        self.assertEqual(case["oracle"]["reviewed_by"], "owner message 7")
        self.assertEqual(case["split"], "development")
        with self.assertRaises(ValueError):
            lesson_cases.fill(self.root, 1, key, "case", None, "changed", [requirement("B")])   # frozen

    def test_recombination_drafts_a_reserved_case_by_code_and_the_trial_uses_the_skills_cases(self):
        a = self.lesson(rule="Keep the owner's value", request="Save result.txt containing A.")
        b = self.lesson(rule="Never add a trailing note", request="Save result.txt containing A and nothing after it.")
        other = self.store.start(1, "Save result.txt containing Z.", "")["id"]
        c = learning.propose(self.root, 1, "A lesson of another skill", str(self.store.directory(other) / "checkpoint.json"), "other")
        with self.assertRaises(ValueError):
            lesson_cases.recombine(self.root, 1, a, [b])                                   # not approved yet
        for key in (a, b):
            lesson_cases.fill(self.root, 1, key, "case", None, "Owner named A", [requirement("A")])
            self.calibrated(key)
        with self.assertRaises(ValueError):
            lesson_cases.recombine(self.root, 1, a, [c])                                   # another skill (no proposal)
        out = lesson_cases.recombine(self.root, 1, a, [b])
        self.assertEqual(out["slot"], "reserved")
        self.assertIn("Save result.txt containing A.\n\nAlso: Never add a trailing note.", out["request"])
        reserved = self.read(a, "reserved")
        self.assertEqual(reserved["split"], "reserved")
        self.assertEqual([r["id"] for r in reserved["requirements"]], ["value", "r2-value"])   # same path, so portable
        self.assertEqual(reserved["from"], [a, b])
        self.calibrated(a, "reserved")
        ids = [c["id"] for c in regression.load_cases(self.root)]
        self.assertIn(a + "-reserved", ids)
        spec = lesson_cases.default_spec(self.root, 1, a, dict(repetitions=1))
        self.assertEqual(spec["development_cases"], sorted([a + "-case", b + "-case"]))
        self.assertEqual(spec["heldout_cases"], [a + "-reserved"])
        chosen = dict(development_cases=["invoice-v1"], heldout_cases=[b + "-case"])
        self.assertEqual(lesson_cases.default_spec(self.root, 1, a, chosen), chosen)        # explicit choice stands
        started = trials.start(self.root, 1, a, dict(repetitions=1, max_steps=5), self.store, "You are the agent.")
        record = learning.read(self.root, 1, a)["trial"]
        self.assertEqual(record["splits"], {a + "-case": "development", b + "-case": "development", a + "-reserved": "heldout"})
        self.assertEqual(len(started["tasks"]), 6)

    def test_the_monitor_grades_calibrations_beside_trials(self):
        src = Path(rt.__file__).read_text(encoding="utf-8")
        self.assertIn("lesson_cases.reconcile(self)", src)


if __name__ == "__main__":
    unittest.main()
