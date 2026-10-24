from agent_harness import put
"""Decision 7 (2026-10-20): Alex answered a pending question at 18:24 while Cato was busy. The message sat in the
inbox queue, so the archive recorded it with an inbox source; task_decision(answer) refused it as "not the user's
conversation", the decision stayed pending, and Cato asked again at 19:56. Now an inbox-sourced user message is a
valid quote, and a pending decision that a later user message or a finished task overlaps is flagged possibly stale."""
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from background_runtime import TaskStore
from agent_core import task_decisions


class DecisionSeven(unittest.TestCase):
    CHAT = 123456789

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = TaskStore(self.root)
        self.task = self.store.start(self.CHAT, "方向不对称检验（预先登记）", "")["id"]
        put(self.store, self.task, "running")
        self.decision = self.store.ask(self.CHAT, self.task, "方向不对称 (a) 拆分需要篮子成立日：Alex 能否导出 270 个篮子的成立/上线日？")
        self.recall = self.root / "task_state/conversation_recall.sqlite3"
        db = sqlite3.connect(self.recall)
        db.execute("CREATE TABLE messages(id INTEGER,chat TEXT,speaker TEXT,text TEXT,source TEXT,said_at TEXT)")
        db.commit(); db.close()

    def message(self, ident, text, source, speaker="user", minutes=1):
        db = sqlite3.connect(self.recall)
        db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?)", (ident, str(self.CHAT), speaker, text, source,
                   (datetime.now() + timedelta(minutes=minutes)).isoformat(timespec="minutes")))
        db.commit(); db.close()

    def test_inbox_stored_user_message_is_a_valid_quote(self):
        self.message(7294, "你无需关心篮子建立日，这些篮子都不是事后创建的", str(self.root / "task_state/inbox.sqlite3") + "#job=1765")
        resolved = self.store.answer(self.CHAT, self.decision["id"], "不需要篮子建立日", 7294, "你无需关心篮子建立日")
        self.assertEqual(resolved["state"], "answered")

    def test_worker_brief_and_assistant_text_are_still_refused(self):
        self.message(1, "你无需关心篮子建立日", str(self.root / "task_state/background/7/conversation_123456789.json"))
        self.message(2, "你无需关心篮子建立日", str(self.root / "task_state/inbox.sqlite3") + "#job=9", speaker="assistant")
        with self.assertRaisesRegex(ValueError, "worker brief"):
            self.store.answer(self.CHAT, self.decision["id"], "x", 1, "你无需关心篮子建立日")
        with self.assertRaisesRegex(ValueError, "Exact user quote"):
            self.store.answer(self.CHAT, self.decision["id"], "x", 2, "你无需关心篮子建立日")

    def test_later_matching_message_flags_the_decision_stale_but_does_not_close_it(self):
        self.assertNotIn("possibly_stale", self.store.decisions(self.CHAT)[0])
        self.message(7294, "你无需关心篮子建立日，这些篮子都不是事后创建的", str(self.root / "task_state/inbox.sqlite3") + "#job=1765")
        row = self.store.decisions(self.CHAT)[0]
        self.assertEqual(row["state"], "pending")
        self.assertIn("user message 7294 at", row["possibly_stale"])
        self.assertRegex(row["possibly_stale"], "mentions (篮子|立日|建立)")
        self.assertIn("possibly_stale", self.store.live_context(self.CHAT))

    def test_unrelated_or_earlier_messages_do_not_flag(self):
        self.message(3, "晚饭八点？", str(self.root / f"conversation_{self.CHAT}.json"))
        self.message(4, "篮子成立日你能导出吗", str(self.root / f"conversation_{self.CHAT}.json"), minutes=-30)
        self.assertNotIn("possibly_stale", self.store.decisions(self.CHAT)[0])

    def test_finished_task_and_newer_task_on_the_topic_flag(self):
        put(self.store, self.task, "finished")
        self.assertIn(f"task {self.task} is finished", self.store.decisions(self.CHAT)[0]["possibly_stale"])
        time.sleep(0.01)
        self.store.start(self.CHAT, "篮子成立日重跑：方向不对称", "")
        self.assertIn("newer task", self.store.decisions(self.CHAT)[0]["possibly_stale"])


if __name__ == "__main__":
    unittest.main()
