"""Lessons That Change Methods, step 2: observations, dockets and method reviews (tabularium.learning.dockets)."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tabularium.learning import learning, dockets
from tabularium.records import background_runtime as rt

SKILL = '---\nname: briefs\nmetadata:\n  version: "1.0.0"\n---\n\n## Acceptance\n- State what done looks like.\n'
DAY = 86400


class Dockets(unittest.TestCase):
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
        touched = dockets.propose_groups(self.root, 1)
        self.assertEqual(len(touched), 1)
        docket = dockets.read(self.root, 1, touched[0])
        self.assertEqual((docket["status"], docket["members"], sorted(docket["unreviewed"])), ("proposed", [], sorted(keys)))
        self.assertNotIn(other, dockets.members_of(docket))
        with self.assertRaisesRegex(ValueError, "No such docket"):
            dockets.group(self.root, 1, "docket-nope", keys)
        out = dockets.group(self.root, 1, touched[0], keys[:2], method_path=self.skill)
        docket = dockets.read(self.root, 1, touched[0])
        self.assertEqual((out["status"], docket["members"], docket["unreviewed"]), ("open", keys[:2], [keys[2]]))
        self.assertEqual(docket["method_path"], str(self.skill))
        with self.assertRaisesRegex(ValueError, "belongs to docket"):          # one observation, one docket
            dockets.group(self.root, 1, "", [keys[0]])
        before = dockets.observed_at(self.root, 1, keys[0])
        out = dockets.ungroup(self.root, 1, touched[0], [keys[0]], note="a different failure: the brief had no acceptance at all")
        self.assertEqual((out["members"], out["unreviewed"]), (1, 1))
        self.assertEqual(learning.read(self.root, 1, keys[0])["status"], "candidate")     # untouched, with its time
        self.assertEqual(dockets.observed_at(self.root, 1, keys[0]), before)
        self.assertNotIn(keys[0], dockets.grouped(self.root, 1))
        state = learning.read(self.root, 1, keys[0])["review"]                            # judged: never proposed again
        self.assertEqual((state["docket"], state["verdict"]), (touched[0], "excluded"))
        self.assertIn("different failure", state["note"])
        self.assertEqual(dockets.propose_groups(self.root, 1), [])
        self.assertNotIn(keys[0], dockets.members_of(dockets.read(self.root, 1, touched[0])))
        self.assertEqual(learning.read(self.root, 1, keys[1])["review"]["verdict"], "member")
        self.assertEqual(learning.read(self.root, 1, keys[2])["review"]["verdict"], "proposed")
        dockets.ungroup(self.root, 1, touched[0], keys[1:])
        self.assertEqual(dockets.read(self.root, 1, touched[0])["status"], "closed")       # emptied: closed, not deleted

    def test_three_observations_in_sixty_days_open_one_method_review_in_the_runtime_lane(self):
        now = 1_800_000_000
        old = self.prose_only(0, at=now - 70 * DAY)
        recent = [self.prose_only(n, at=now - n * DAY) for n in (1, 2)]
        dockets.propose_groups(self.root, 1, now)
        self.assertEqual(dockets.flush(self.agent, now), [])                                 # two recent: below the threshold
        third = self.prose_only(3, at=now - 30 * DAY)
        started = dockets.flush(self.agent, now)
        self.assertEqual(len(started), 1)
        task = self.store.get(started[0])
        self.assertTrue(task["request"].startswith(dockets.PREFIX))
        self.assertTrue(rt.meta(task["request"]))                                            # the runtime lane
        from tabularium.learning.corrections import is_diagnosis
        self.assertTrue(is_diagnosis(task["request"]))                                       # record-only: no questions
        for k in (old, *recent, third):
            self.assertIn(k, task["request"])
        key = next(k for k, d in dockets.dockets(self.root, 1) if d.get("review_task") == started[0])
        docket = dockets.read(self.root, 1, key)
        self.assertEqual(docket["status"], "review")
        self.assertEqual(dockets.flush(self.agent, now), [])                                 # never queued twice
        fourth = self.prose_only(4, at=now - 5 * DAY)                                        # attaches, unreviewed; no new line
        self.assertEqual(dockets.flush(self.agent, now), [])
        docket = dockets.read(self.root, 1, key)
        self.assertIn(fourth, docket["unreviewed"])
        digest = learning.digest(self.root, 1)
        self.assertIn(f"x5 {key}", digest); self.assertIn("method review open", digest)
        self.assertNotIn(fourth, digest)
        self.assertEqual(learning.expire(self.root, 1, days=30, now=now + 40 * DAY), [])     # evidence never expires
        self.assertEqual(learning.read(self.root, 1, old)["status"], "candidate")
        # A second round needs three observations proposed since the last review started, not a recount.
        self.store.control(1, started[0], "cancel", "settled for the test")
        self.assertEqual(dockets.flush(self.agent, now + 60), [])                            # one new (the fourth): no round
        dockets.ungroup(self.root, 1, key, [fourth], note="unrelated")
        for n in (5, 6):
            self.prose_only(n, at=now - n * DAY)
        self.assertEqual(dockets.flush(self.agent, now + 120), [])                           # two new, one excluded: no round
        self.prose_only(7, at=now - 7 * DAY)
        again = dockets.flush(self.agent, now + 180)
        self.assertEqual(len(again), 1)
        self.assertEqual(len(dockets.read(self.root, 1, key)["reviews"]), 2)
        self.assertEqual(dockets.flush(self.agent, now + 240), [])

    def test_an_applied_proposal_that_explains_the_members_closes_the_theme(self):
        keys = [self.prose_only(n) for n in range(3)]
        dockets.propose_groups(self.root, 1)
        key = dockets.dockets(self.root, 1)[0][0]
        dockets.group(self.root, 1, key, keys, method_path=self.skill)
        learning.propose_change(self.root, 1, keys[0], self.skill, "- State what done looks like.",
                                "- State done as a check a script can run: a file, a value, a command's exit code.",
                                {"claim": "The acceptance rule allows prose, so a worker cannot verify its own result.",
                                 "anchor": "- State what done looks like."}, explains=keys[1:])
        learning.approve(self.root, 1, keys[0], 7, "yes", skip_trial=True)
        docket = dockets.read(self.root, 1, key)
        self.assertEqual((docket["status"], docket["closed"]["by"]), ("closed", keys[0]))
        self.assertEqual([r["key"] for r in dockets.reviews(self.root, 1)], [])

    def test_migration_relabels_unreceipted_checks_and_proposes_the_grouping_once(self):
        key = self.prose_only(0)
        learning.propose_change(self.root, 1, key, self.skill, "", "Say what done looks like.", {"claim": "c", "silent_at": "acceptance"})
        learning.approve(self.root, 1, key, 7, "yes", skip_trial=True)
        learning._update(self.root, 1, key, lambda d: d.update(checks=[{"worked": True, "evidence": "old style", "at": 1}], helped=1, hurt=0, status="verified"))
        for n in (1, 2):
            self.prose_only(n)
        note = self.observe("An October lesson whose checks are one note", 8)      # model-written: checks is a string
        learning._update(self.root, 1, note, lambda d: d.update(checks="Checked on the Tiro task: fine"))
        out = dockets.migrate(self.root, 1)
        self.assertEqual(out["relabelled"], 2)
        self.assertEqual(learning.read(self.root, 1, note)["checks"], [{"worked": None, "evidence": "Checked on the Tiro task: fine", "judgement": True, "at": learning.read(self.root, 1, note)["checks"][0]["at"]}])
        d = learning.read(self.root, 1, key)
        self.assertTrue(d["checks"][0]["judgement"]); self.assertEqual((d["helped"], d["hurt"], d["status"]), (0, 0, "verified"))
        self.assertEqual(len(out["dockets"]), 1)
        self.assertEqual(dockets.migrate(self.root, 1), {"relabelled": 0, "marked": 0, "dockets": []})      # idempotent
        docket = out["dockets"][0]
        members = dockets.members_of(dockets.read(self.root, 1, docket))
        dockets.ungroup(self.root, 1, docket, members[:1], note="old-style exclusion")
        for k in members:                                                                    # records from before review states
            learning._update(self.root, 1, k, lambda d: d.pop("review", None))
        t = dockets.read(self.root, 1, docket); t["unreviewed"] = members; dockets._save(self.root, 1, docket, t)
        out = dockets.migrate(self.root, 1)
        self.assertEqual((out["marked"], out["dockets"]), (len(members), []))
        self.assertEqual(learning.read(self.root, 1, members[0])["review"]["verdict"], "excluded")
        self.assertEqual(learning.read(self.root, 1, members[1])["review"]["verdict"], "proposed")
        self.assertNotIn(members[0], dockets.read(self.root, 1, docket)["unreviewed"])


if __name__ == "__main__":
    unittest.main()
