"""Weekly memory exam (2026-10-11): sources are Alex's own recent statements, the examiner writes and grades,
the agent answers through his turn, and the record says what was remembered."""
import json, sys, tempfile, unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS
ROOT = Path(__file__).resolve().parents[1]
from tabularium.memory import memory_exam as me
from tabularium.memory.conversation_recall import ConversationRecall


def seed(recall, rows):
    from contextlib import closing
    with closing(recall.connect()) as db, db:
        for i, (speaker, text, said_at) in enumerate(rows, 1):
            db.execute("INSERT INTO messages(id,fingerprint,chat,speaker,said_at,date_kind,text,source,recorded_at) VALUES (?,?,?,?,?,?,?,?,?)",
                       (i, f"f{i}", "1", speaker, said_at, "exact", text, "test", 0))


class Sources(unittest.TestCase):
    def test_recent_user_statements_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            recall = ConversationRecall(tmp)
            seed(recall, [("user", "I will be in Tokyo next week for the client conference, back on Friday.", "2026-10-09 10:00"),
                          ("user", "What is the weather like tomorrow in Paris right now please?", "2026-10-09 10:01"),
                          ("user", "/tasks", "2026-10-09 10:02"),
                          ("assistant", "Noted: Tokyo next week, back Friday. Anything to prepare?", "2026-10-09 10:03"),
                          ("user", "ok", "2026-10-09 10:04"),
                          ("user", "The new report file lives on the shared drive under workflow, not in the old reports folder.", "2026-09-01 10:00")])
            picked = me.pick_sources(recall, 1, now=datetime(2026, 10, 11, 12, 0))
            self.assertEqual([p["id"] for p in picked], [1])            # statement in range; question, command, short, old, assistant excluded


class Exam(unittest.TestCase):
    def test_exam_grades_answers(self):
        sources = [{"id": 1, "text": "I will be in Tokyo next week.", "said_at": "2026-10-09"},
                   {"id": 2, "text": "Call me Alex, not by a nickname.", "said_at": "2026-10-09"},
                   {"id": 3, "text": "thanks that is all for now really", "said_at": "2026-10-10"}]
        questions = {"I will be in Tokyo next week.": {"question": "Where does Alex travel next week?", "expected": "Tokyo"},
                     "Call me Alex, not by a nickname.": {"question": "How must Alex be addressed?", "expected": "Alex, not by a nickname"}}
        grades = {"Tokyo": {"score": "correct", "note": "named Tokyo"}, "I do not remember.": {"score": "wrong", "note": "did not know"}}

        def examiner(prompt, schema):
            if prompt.startswith("Alex wrote"):
                text = prompt.split("<message>\n", 1)[1].split("\n</message>")[0]
                return json.dumps(questions.get(text, {"skip": True}))
            answer = prompt.split("Assistant's answer: ", 1)[1].split("\n", 1)[0]
            return json.dumps(grades[answer])

        def ask(question):
            return ("Tokyo", ["read_memory"]) if "travel" in question else ("I do not remember.", [])

        record = me.run_exam(NS(), 1, examiner=examiner, ask=ask, sources=sources)
        self.assertEqual((record["correct"], record["partial"], record["wrong"], record["skipped"]), (1, 0, 1, 1))
        self.assertFalse(record["ok"])
        self.assertIn("1/2 correct", me.summary(record))
        with tempfile.TemporaryDirectory() as tmp:
            me.save(tmp, record)
            self.assertTrue((Path(tmp) / "task_state" / "memory_exam.json").exists())

    def test_examiner_failure_is_recorded_not_raised(self):
        def examiner(prompt, schema): raise RuntimeError("API down")
        record = me.run_exam(NS(), 1, examiner=examiner, ask=lambda q: ("", []), sources=[{"id": 1, "text": "x" * 50, "said_at": "2026-10-09"}])
        self.assertFalse(record["ok"]); self.assertIn("RuntimeError", record["error"])

    def test_nothing_to_examine_passes(self):
        record = me.run_exam(NS(), 1, examiner=lambda p: "{}", ask=lambda q: ("", []), sources=[])
        self.assertTrue(record["ok"]); self.assertEqual(me.summary(record), "nothing to examine this week")


if __name__ == "__main__":
    unittest.main()
