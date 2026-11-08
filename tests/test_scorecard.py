"""The weekly scorecard counts only what was recorded, per local week (2026-10-16)."""
import contextlib, io, json, sqlite3, tempfile, unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from tabularium.ops import scorecard
from tabularium.core.turn_loop import Exchange
from tabularium.core import turn_loop
from agent_harness import make_agent

NOW = datetime(2026, 10, 16, 20, 0, tzinfo=scorecard.LOCAL)       # a Friday
THIS = NOW.timestamp(); LAST = (NOW - timedelta(days=7)).timestamp()


def seed(root, name):
    state = root / name / "task_state"; (state / "background").mkdir(parents=True)
    with closing(sqlite3.connect(state / "inbox.sqlite3")) as db, db:
        db.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, chat INTEGER, message INTEGER, handler TEXT, payload TEXT, status TEXT, created REAL)")
        db.execute("CREATE TABLE usage_events (id INTEGER PRIMARY KEY, job INTEGER, created REAL, kind TEXT, provider TEXT, model TEXT, billing TEXT, role TEXT, metrics TEXT)")
        for i, (status, created) in enumerate([("done", THIS), ("done", THIS - 60), ("failed", THIS), ("done", LAST)]):
            db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?)", (i + 1, 1, i + 1, "handle_message", "{}", status, created))
        db.execute("INSERT INTO usage_events(created,kind,model,role) VALUES (?,?,?,?)", (THIS, "bounce", "unbacked_promise", "runtime"))
        db.execute("INSERT INTO usage_events(created,kind,model,role) VALUES (?,?,?,?)", (LAST, "bounce", "runaway", "runtime"))
        db.execute("INSERT INTO usage_events(created,kind,model,role) VALUES (?,?,?,?)", (THIS, "response", "m", "worker"))
    with closing(sqlite3.connect(state / "background" / "jobs.sqlite3")) as db, db:
        db.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, chat INTEGER, request TEXT, status TEXT, created REAL, updated REAL, pid INTEGER, note TEXT, notified INTEGER)")
        db.execute("INSERT INTO tasks(chat,request,status,created,updated) VALUES (1,'a','finished',?,?)", (THIS - 1200, THIS))
        db.execute("INSERT INTO tasks(chat,request,status,created,updated) VALUES (1,'b','needs_attention',?,?)", (THIS - 600, THIS))
        db.execute("CREATE TABLE scorecard (request_id TEXT, agent TEXT, items_total INTEGER, items_met INTEGER, reworks INTEGER, guesses_verdict TEXT, accepted REAL)")
        db.execute("INSERT INTO scorecard VALUES ('r1','vit',4,3,1,'ok',?)", (THIS,))
        db.execute("INSERT INTO scorecard VALUES ('r2','cal',2,2,0,'wrong',?)", (THIS,))
    with closing(sqlite3.connect(state / "projects.sqlite3")) as db, db:
        db.execute("CREATE TABLE records (chat INTEGER, kind TEXT, key TEXT, version INTEGER, data TEXT, updated REAL)")
        db.execute("INSERT INTO records VALUES (1,'lesson','l1',1,'{\"status\": \"candidate\"}',?)", (THIS,))
        db.execute("INSERT INTO records VALUES (1,'lesson','l2',1,'{\"status\": \"applied\"}',?)", (THIS,))
        db.execute("INSERT INTO records VALUES (1,'project','p1',1,'{}',?)", (THIS,))
    (state / "memory_exam.json").write_text(json.dumps({"time": THIS, "correct": 2, "partial": 0, "wrong": 1}), encoding="utf-8")


class Scorecard(unittest.TestCase):
    def test_week_bounds_are_local_mondays(self):
        start, end = scorecard.week_bounds(NOW)
        self.assertEqual(datetime.fromtimestamp(start, scorecard.LOCAL).strftime("%a %d %H:%M"), "Mon 12 00:00")
        self.assertEqual((end - start) / 3600, 24 * 7)
        last, _ = scorecard.week_bounds(NOW, 1)
        self.assertEqual(datetime.fromtimestamp(last, scorecard.LOCAL).day, 5)

    def test_counts_this_week_and_last_from_records_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); seed(root, "cato")
            for other in ("tacitus", "callimachus", "vitruvius"):
                (root / other / "task_state").mkdir(parents=True)
            (root / "docs" / "incidents").mkdir(parents=True)
            (root / "docs" / "incidents" / "2026-10-15-x.md").write_text("# x\n| Affected | Cato; Vit's client |\n", encoding="utf-8")
            report = scorecard.weekly(root, now=NOW)
            this, last = (w["agents"]["cato"] for w in report["weeks"])
            self.assertEqual((this["turns"], this["turns_failed"], last["turns"]), (2, 1, 1))
            self.assertEqual((this["bounces"], last["bounces"]), ({"unbacked_promise": 1}, {"runaway": 1}))
            self.assertEqual((this["tasks_finished"], this["tasks_stopped"], this["task_minutes_median"]), (1, 1, 20.0))
            self.assertEqual(this["delegations"], {"accepted": 2, "items_met": 5, "items_total": 6, "reworks": 1, "wrong_guesses": 1,
                                               "rework_changed": 0, "rework_unchanged": 0})
            self.assertEqual((this["memory_exam"], this["lessons_candidate"], this["lessons_taken"], this["incidents"]),
                             ({"correct": 2, "partial": 0, "wrong": 1}, 1, 1, 1))
            self.assertEqual(report["weeks"][0]["agents"]["vitruvius"]["incidents"], 1)      # "Vit" in the Affected row
            self.assertEqual(report["weeks"][0]["agents"]["tacitus"]["turns"], 0)             # no records, no reads
            self.assertIn("lessons cand/taken + inc", scorecard.table(report))

    def test_the_runtime_records_a_bounce_when_it_happens(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "cato"; root.mkdir()
            with contextlib.redirect_stdout(io.StringIO()):
                agent = make_agent("cato", root)
            agent.BACKGROUND_TASK_ID = ""
            script = [Exchange(text='{"reply": "我会研究。", "work": "started"}'), Exchange(text='{"reply": "要开始吗？", "work": "awaiting_go"}')]
            provider = type("P", (), {"exchange": lambda self, m: script.pop(0)})()
            turn_loop.run(agent, [{"role": "user", "content": "hi"}], 1, provider)
            with closing(sqlite3.connect(agent._inbox.database)) as db:
                self.assertEqual(db.execute("SELECT model FROM usage_events WHERE kind='bounce'").fetchall(), [("unbacked_promise",)])


if __name__ == "__main__":
    unittest.main()
