"""Audit round 3 (2026-10-08): decisions carry Alex's words, route line on results,
repeated lesson surfaces as OPEN when no skill changed."""
import sys, tempfile, unittest, json, sqlite3
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
from tabularium.records import background_runtime as rt
class Decisions(unittest.TestCase):
    def test_owner_needs_quote(self):
        self.assertEqual(rt.unquoted_owner_decisions(["Alex: top 20 only", "Cato: FY June"]), "Alex: top 20 only")
        self.assertEqual(rt.unquoted_owner_decisions(["Alex: \u201ctop 20 only\u201d", "System: no ceiling"]), "")
        self.assertEqual(rt.unquoted_owner_lines("Decisions:\n- Alex: fast\n- alex: \"right\"\nCato: x"), "Alex: fast")

    def test_checkpoint_refuses_unquoted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = rt.TaskStore(Path(tmp))
            with store.db() as db:
                db.execute("INSERT INTO tasks(id,chat,request,status,created,updated) VALUES (1,1,'r','running',0,0)")
            store.directory(1).mkdir(parents=True, exist_ok=True)
            with self.assertRaises(ValueError):
                store.checkpoint(1, 1, "a", "b", [], decisions=["Alex: skip the verifier"])
            store.checkpoint(1, 1, "a", "b", [], decisions=["Alex: \"skip the verifier\""])


class ResultLines(unittest.TestCase):
    def test_route_line_and_repeat_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = rt.TaskStore(Path(tmp))
            folder = store.directory(3)
            (folder / "task_state").mkdir(parents=True, exist_ok=True)
            self.assertEqual(store.route_line(3), "")
            db = sqlite3.connect(folder / "task_state" / "inbox.sqlite3")
            db.execute("CREATE TABLE usage_events(id INTEGER PRIMARY KEY, kind TEXT, provider TEXT, billing TEXT)")
            db.executemany("INSERT INTO usage_events(kind,provider,billing) VALUES (?,?,?)",
                           [("response", "openai", "subscription")] * 3 + [("response", "claude-sdk", "subscription")])
            db.commit(); db.close()
            self.assertEqual(store.route_line(3), "[Route] openai/subscription 3 turns; claude-sdk/subscription 1 turns")
            self.assertEqual(store.repeat_lesson_open(3), "")
            (folder / "checkpoint.json").write_text(json.dumps({"repeat_lesson": {"line": "old format"}}), encoding="utf-8")
            self.assertEqual(store.repeat_lesson_open(3), "")
            (folder / "checkpoint.json").write_text(json.dumps({"repeat_lesson": {"key": "lesson-abc", "time": 1.0}}), encoding="utf-8")
            self.assertIn("lesson-abc", store.repeat_lesson_open(3))        # flagged for Alex; nothing is approved by it



if __name__ == "__main__":
    unittest.main()
