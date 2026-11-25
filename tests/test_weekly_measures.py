"""The four numbers a change to the framework is judged by, and the cost a delegation records (2026-11-25).

A review of seven weeks of records found sixty design changes and no measure that said which of them helped: the share
of turns the owner corrected was not shown, a turn's cost was not shown, how often he was asked was not shown, and
the cost of a delegated request against its target had never been written (the column was empty in all 109 requests,
while real cost ran ten times the targets). The weekly scorecard now carries all four, from records only."""
import json, sqlite3, tempfile, unittest
from contextlib import closing
from pathlib import Path
from agent_harness import make_agent
from tabularium.ops import scorecard
from test_delegation_agreement import bind_delegate
from test_scorecard import LAST, NOW, THIS, seed


class WeeklyMeasures(unittest.TestCase):
    def report(self, extend=None):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root = Path(tmp.name); seed(root, "coordinator")
        for other in ("private", "librarian", "engineer"):
            (root / other / "task_state").mkdir(parents=True)
        state = root / "coordinator" / "task_state"
        if extend:
            extend(state)
        return [w["agents"]["coordinator"] for w in scorecard.weekly(root, now=NOW)["weeks"]], scorecard.weekly(root, now=NOW)

    def test_correction_rate_tokens_per_turn_and_asks(self):
        def extend(state):
            with closing(sqlite3.connect(state / "inbox.sqlite3")) as db, db:
                db.execute("INSERT INTO usage_events(created,kind,model,role) VALUES (?,?,?,?)", (THIS, "correction", "owner", "foreground"))
                db.execute("INSERT INTO usage_events(created,kind,model,role,metrics) VALUES (?,?,?,?,?)", (THIS, "response", "m", "sdk_total",
                           json.dumps({"input_uncached": 1000, "cache_read": 10000, "cache_write": 0, "output": 100})))
            with closing(sqlite3.connect(state / "background" / "jobs.sqlite3")) as db, db:
                db.execute("CREATE TABLE decisions (id INTEGER PRIMARY KEY, created REAL)")
                db.executemany("INSERT INTO decisions(created) VALUES (?)", [(THIS,), (THIS - 60,), (THIS - 120,), (LAST,)])
        (this, last), report = self.report(extend)
        self.assertEqual(this["correction_rate"], 0.5)               # one correction over the two turns answered
        self.assertEqual(this["tokens_per_turn"], 1250)              # (1000 + 10000 x 0.1 + 100 x 5) over two turns
        self.assertEqual((this["asked"], last["asked"]), (3, 1))
        self.assertEqual(last["correction_rate"], 0.0)
        text = scorecard.table(report)
        self.assertIn("1 (50%)", text)
        self.assertIn("1,250", text)

    def test_a_week_without_turns_has_no_rate(self):
        (this, last), report = self.report()
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        for name in ("coordinator", "private", "librarian", "engineer"):
            (Path(tmp.name) / name / "task_state").mkdir(parents=True)
        empty = scorecard.weekly(Path(tmp.name), now=NOW)
        week = empty["weeks"][0]["agents"]["coordinator"]
        self.assertIsNone(week["correction_rate"])
        self.assertIsNone(week["tokens_per_turn"])
        self.assertEqual(week["asked"], 0)
        self.assertIn("tokens per turn", scorecard.table(empty))

    def test_delegation_cost_counts_only_requests_that_recorded_one(self):
        def extend(state):
            with closing(sqlite3.connect(state / "background" / "jobs.sqlite3")) as db, db:
                db.execute("ALTER TABLE scorecard ADD COLUMN budget INTEGER")
                db.execute("ALTER TABLE scorecard ADD COLUMN spent INTEGER")
                db.execute("UPDATE scorecard SET budget=200000, spent=1000000 WHERE request_id='r1'")
                db.execute("UPDATE scorecard SET budget=300000 WHERE request_id='r2'")          # reviewed before costs were recorded
        (this, _), report = self.report(extend)
        self.assertEqual(this["delegation_cost"], {"priced": 1, "spent": 1000000, "target": 200000})
        self.assertIn("5.0x (1)", scorecard.table(report))

    def test_no_priced_request_shows_no_cost(self):
        (this, _), report = self.report()
        self.assertIsNone(this["delegation_cost"])


class ReviewRecordsCost(unittest.TestCase):
    def test_the_review_writes_what_the_delegate_task_has_spent(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "coordinator"; root.mkdir()
        store = make_agent("coordinator", root)._background
        task = store.start(1, "Owner brief", completion_items=[dict(item="Synthesis", check="Read it", critical=True)])["id"]
        items = [dict(item="Explain tradeoffs", check="Assess reasoning", critical=True, early=True)]
        store.add_delegation(task, "cal", "req-cost", "research", "worker_judgment")
        store.record_submission("req-cost", task, "cal", "research", 250000, items)
        remote, child = bind_delegate(store, task, "req-cost", items, "cal")
        usage = remote.directory(child) / "task_state"; usage.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(usage / "inbox.sqlite3")) as db, db:
            db.execute("CREATE TABLE usage_events (id INTEGER PRIMARY KEY, created REAL, kind TEXT, role TEXT, metrics TEXT)")
            for metrics in ({"input_uncached": 2000, "cache_read": 30000, "cache_write": 1000, "output": 400},
                            {"input_uncached": 500, "cache_read": 0, "cache_write": 0, "output": 100}):
                db.execute("INSERT INTO usage_events(created,kind,role,metrics) VALUES (1,'response','worker',?)", (json.dumps(metrics),))
        delivery = store.save_delegate_result(task, "req-cost", "DONE: delivered result")
        self.assertIsNone(store.scorecard("req-cost")["spent"])
        store.accept_delegation(task, "req-cost", [{"item": 1, "verdict": "met", "how": "read", "evidence": "read the argument and both tradeoffs in the result"}], delivery_id=delivery)
        # 2000 + 30000 x 0.1 + 1000 x 1.25 + 400 x 5, then 500 + 100 x 5
        self.assertEqual(store.scorecard("req-cost")["spent"], 8250 + 1000)


if __name__ == "__main__":
    unittest.main()
