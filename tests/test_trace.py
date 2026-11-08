"""The trace of a task is built from receipts and usage rows that already exist, in time order (2026-10-20)."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from tabularium.core import trace
class Trace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name) / "7"
        (self.folder / "task_state" / "actions").mkdir(parents=True)
        self.store = SimpleNamespace(directory=lambda ident: self.folder)

    def tearDown(self):
        self.tmp.cleanup()

    def receipt(self, ns, tool, status="finished", result="x" * 50, inputs=None):
        (self.folder / "task_state" / "actions" / f"{ns}.json").write_text(json.dumps(
            {"tool": tool, "inputs": inputs or {"q": "iphone"}, "status": status, "result": result}), encoding="utf-8")

    def test_tools_models_and_checkpoint_in_time_order(self):
        self.receipt(2_000_000_000_000_000_000, "web_search")
        self.receipt(4_000_000_000_000_000_000, "read_file", status="failed", result="", inputs={"path": "x"})
        db = self.folder / "task_state" / "inbox.sqlite3"
        with closing(sqlite3.connect(db)) as con:
            con.execute("CREATE TABLE usage_events(id INTEGER PRIMARY KEY, created REAL, kind TEXT, model TEXT, billing TEXT, role TEXT, metrics TEXT)")
            con.execute("INSERT INTO usage_events(created,kind,model,billing,role,metrics) VALUES (3e9,'response','claude-x','sub','worker','{\"input_uncached\":100,\"output\":20,\"cache_read\":500,\"cache_write\":0}')")
            con.execute("INSERT INTO usage_events(created,kind,model,billing,role,metrics) VALUES (1e9,'tool_started','claude-x','sub','worker','{}')")
            con.commit()
        (self.folder / "checkpoint.json").write_text(json.dumps({"updated": 5e9, "completed": "done", "blocker": ""}), encoding="utf-8")
        evs = trace.events(self.store, 7)
        self.assertEqual([e["kind"] for e in evs], ["tool", "model", "tool", "checkpoint"])
        self.assertEqual(evs[1]["size"], 620)                      # all provider-reported tokens, not a billing estimate
        self.assertEqual(evs[0]["detail"], "q=iphone")
        s = trace.summary(evs)
        self.assertEqual((s["tool_calls"], s["failed_tools"], s["model_calls"], s["tokens"]), (2, 1, 1, 620))

    def test_openai_usage_is_counted_once(self):
        # Codex review 2026-10-20: 1,000 input + 100 output showed as 1,500.
        record = {"input_total": 1000, "cache_read": 0, "input_uncached": 1000, "output": 100}
        self.assertEqual(trace.reported_tokens(record), 1100)
        self.assertEqual(trace.reported_tokens({"input_total": 1000, "cache_read": 400, "output": 100}), 1100)
        self.assertEqual(trace.reported_tokens({"input_uncached": 10, "cache_write": 50, "cache_read": 900, "output": 5}), 965)

    def test_archived_output_is_named(self):
        self.receipt(1_000_000_000_000_000_000, "execute_command", result="[Archived tool output: D:/x/out.txt] head")
        self.assertEqual(trace.events(self.store, 7)[0]["archive"], "D:/x/out.txt")

    def test_empty_task(self):
        self.assertEqual(trace.events(self.store, 7), [])
        self.assertEqual(trace.summary([])["minutes"], 0)


if __name__ == "__main__":
    unittest.main()
