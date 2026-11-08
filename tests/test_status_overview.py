"""Old snapshots of background state do not come back as current (2026-10-12).

At 12:52 a research call was handed to background task 148, and the hand-off copied Alex's whole composed message,
including the runtime's live-work line "#143 needs_attention". Task 143 was cancelled at 13:54. At 16:02 Cato asked
for status, which returned all ten recent briefs in full (23 KB), and he reported the recap job as needing attention.
Status now returns one short row per task; the hand-off side is in test_turn_notes.py."""
import json, sys, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
from tabularium.records import background_runtime as rt
STALE = "[Background work: #143 needs_attention: Rewrite this week's US & Europe weekly recap; task_evidence(id) for details]"


class Overview(unittest.TestCase):
    def test_status_rows_are_short_and_leave_the_brief_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = rt.TaskStore(Path(tmp))
            brief = "Carry out Alex's current request: 还有什么有意思的主题\n" + STALE + "\n" + "x" * 3000
            with store.db() as db:
                db.execute("INSERT INTO tasks(id,chat,request,status,created,updated,note) VALUES (148,1,?,'finished',1791812033,1791815417,'Response saved.')", (brief,))
            out = store.overview(1)
            text = json.dumps(out, ensure_ascii=False)
            self.assertNotIn("#143", text)
            self.assertLess(len(text), 600)
            row = out["tasks"][0]
            self.assertEqual((row["id"], row["status"]), (148, "finished"))
            self.assertTrue(row["label"].startswith("Carry out Alex's current request"))
            self.assertIn("task_evidence", out["detail"])


if __name__ == "__main__":
    unittest.main()
