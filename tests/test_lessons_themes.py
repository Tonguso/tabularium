"""Lessons That Change Methods, step 2: observations, themes and method reviews (tabularium.learning.themes)."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tabularium.learning import learning, themes
from tabularium.records import background_runtime as rt

SKILL = '---\nname: briefs\nmetadata:\n  version: "1.0.0"\n---\n\n## Acceptance\n- State what done looks like.\n'
DAY = 86400


class Themes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        (self.root / "task_state").mkdir()
        con = sqlite3.connect(self.root / "task_state" / "conversation_recall.sqlite3")
        con.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, chat TEXT, speaker TEXT, text TEXT)")
        con.execute("INSERT INTO messages VALUES (7, '1', 'user', 'yes')")
        con.commit(); con.close()
        self.skill = self.root / "skills" / "briefs" / "SKILL.md"
        self.skill.parent.mkdir(parents=True); self.skill.write_text(SKILL, encoding="utf-8")
        self.store = rt.TaskStore(self.root)
        self.agent = SimpleNamespace(AGENT_DIR=self.root, _background=self.store)
        self.store.role = SimpleNamespace(learns=True)

    def tearDown(self):
        self.tmp.cleanup()

    def observe(self, text, n, at=None):
        task = self.store.start(1, f"brief {n}", "")["id"]
        key = learning.propose(self.root, 1, text, str(self.store.directory(task) / "checkpoint.json"), "other")
        if at is not None:
            learning._update(self.root, 1, key, lambda d: d.update(created=at))
        return key

    def prose_only(self, n, **kw):
        return self.observe(f"Brief {n} stated acceptance in prose only, so the result could not be checked by a script", n, **kw)

    def test_the_runtime_proposes_membership_and_only_the_agent_confirms_or_reverses_it(self):
        keys = [self.prose_only(n) for n in range(3)]
        other = self.observe("Sent the workbook before the owner had seen the membership", 9)
        touched = themes.propose_groups(self.root, 1)
        self.assertEqual(len(touched), 1)
        theme = themes.read(self.root, 1, touched[0])
        self.assertEqual((theme["status"], theme["members"], sorted(theme["unreviewed"])), ("proposed", [], sorted(keys)))
        self.assertNotIn(other, themes.members_of(theme))
        with self.assertRaisesRegex(ValueError, "No such theme"):
            themes.group(self.root, 1, "theme-nope", keys)
        out = themes.group(self.root, 1, touched[0], keys[:2], method_path=self.skill)
        theme = themes.read(self.root, 1, touched[0])
        self.assertEqual((out["status"], theme["members"], theme["unreviewed"]), ("open", keys[:2], [keys[2]]))
        self.assertEqual(theme["method_path"], str(self.skill))
        with self.assertRaisesRegex(ValueError, "belongs to theme"):          # one observation, one theme
            themes.group(self.root, 1, "", [keys[0]])
        before = themes.observed_at(self.root, 1, keys[0])
        out = themes.ungroup(self.root, 1, touched[0], [keys[0]], note="a different failure: the brief had no acceptance at all")
        self.assertEqual((out["members"], out["unreviewed"]), (1, 1))
        self.assertEqual(learning.read(self.root, 1, keys[0])["status"], "candidate")     # untouched, with its time
        self.assertEqual(themes.observed_at(self.root, 1, keys[0]), before)
        self.assertNotIn(keys[0], themes.grouped(self.root, 1))
        themes.ungroup(self.root, 1, touched[0], keys[1:])
        self.assertEqual(themes.read(self.root, 1, touched[0])["status"], "closed")       # emptied: closed, not deleted

    def test_three_observations_in_sixty_days_open_one_method_review_in_the_runtime_lane(self):
        now = 1_800_000_000
        old = self.prose_only(0, at=now - 70 * DAY)
        recent = [self.prose_only(n, at=now - n * DAY) for n in (1, 2)]
        themes.propose_groups(self.root, 1, now)
        self.assertEqual(themes.flush(self.agent, now), [])                                 # two recent: below the threshold
        third = self.prose_only(3, at=now - 30 * DAY)
        started = themes.flush(self.agent, now)
        self.assertEqual(len(started), 1)
        task = self.store.get(started[0])
        self.assertTrue(task["request"].startswith(themes.PREFIX))
        self.assertTrue(rt.meta(task["request"]))                                            # the runtime lane
        from tabularium.learning.corrections import is_diagnosis
        self.assertTrue(is_diagnosis(task["request"]))                                       # record-only: no questions
        for k in (old, *recent, third):
            self.assertIn(k, task["request"])
        key = next(k for k, d in themes.themes(self.root, 1) if d.get("review_task") == started[0])
        theme = themes.read(self.root, 1, key)
        self.assertEqual(theme["status"], "review")
        self.assertEqual(themes.flush(self.agent, now), [])                                 # never queued twice
        fourth = self.prose_only(4, at=now - 5 * DAY)                                        # attaches, unreviewed; no new line
        self.assertEqual(themes.flush(self.agent, now), [])
        theme = themes.read(self.root, 1, key)
        self.assertIn(fourth, theme["unreviewed"])
        digest = learning.digest(self.root, 1)
        self.assertIn(f"x5 {key}", digest); self.assertIn("method review open", digest)
        self.assertNotIn(fourth, digest)
        self.assertEqual(learning.expire(self.root, 1, days=30, now=now + 40 * DAY), [])     # evidence never expires
        self.assertEqual(learning.read(self.root, 1, old)["status"], "candidate")

    def test_an_applied_proposal_that_explains_the_members_closes_the_theme(self):
        keys = [self.prose_only(n) for n in range(3)]
        themes.propose_groups(self.root, 1)
        key = themes.themes(self.root, 1)[0][0]
        themes.group(self.root, 1, key, keys, method_path=self.skill)
        learning.propose_change(self.root, 1, keys[0], self.skill, "- State what done looks like.",
                                "- State done as a check a script can run: a file, a value, a command's exit code.",
                                {"claim": "The acceptance rule allows prose, so a worker cannot verify its own result.",
                                 "anchor": "- State what done looks like."}, explains=keys[1:])
        learning.approve(self.root, 1, keys[0], 7, "yes", skip_trial=True)
        theme = themes.read(self.root, 1, key)
        self.assertEqual((theme["status"], theme["closed"]["by"]), ("closed", keys[0]))
        self.assertEqual([r["key"] for r in themes.reviews(self.root, 1)], [])

    def test_migration_relabels_unreceipted_checks_and_proposes_the_grouping_once(self):
        key = self.prose_only(0)
        learning.propose_change(self.root, 1, key, self.skill, "", "Say what done looks like.", {"claim": "c", "silent_at": "acceptance"})
        learning.approve(self.root, 1, key, 7, "yes", skip_trial=True)
        learning._update(self.root, 1, key, lambda d: d.update(checks=[{"worked": True, "evidence": "old style", "at": 1}], helped=1, hurt=0, status="verified"))
        for n in (1, 2):
            self.prose_only(n)
        out = themes.migrate(self.root, 1)
        self.assertEqual(out["relabelled"], 1)
        d = learning.read(self.root, 1, key)
        self.assertTrue(d["checks"][0]["judgement"]); self.assertEqual((d["helped"], d["hurt"], d["status"]), (0, 0, "verified"))
        self.assertEqual(len(out["themes"]), 1)
        self.assertEqual(themes.migrate(self.root, 1), {"relabelled": 0, "themes": []})      # idempotent


if __name__ == "__main__":
    unittest.main()
