"""Per-turn notes live on the wire copy only, never in saved history (2026-10-12).

The language hint, the live background-work line, the silent checkpoint and the memory-review note describe one
moment. They used to be saved inside Alex's message, so every later reader of the history (the research hand-off,
the status list, conversation recall) could bring them back as if current: at 16:02 Cato reported a recap job as
needing attention two hours after it was cancelled, from a '"recorded_state": "needs_attention"' line copied at 12:52. Now
compose_turn saves Alex's words only and registers the notes; trim_history attaches them to that one message in the
request it builds, for every model route."""
import sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
from agent_harness import load_agent


def message(text, date=1791813120):
    return SimpleNamespace(chat=SimpleNamespace(id=1), text=text, date=date, reply_to_message=None)


class TurnNotes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "cato"; root.mkdir()
        self.runtime = load_agent("cato", root)["runtime"]
        self.runtime.BACKGROUND_TASK_ID = ""
        with self.runtime._background.db() as db:
            db.execute("INSERT INTO tasks(id,chat,request,status,created,updated) "
                       "VALUES (143,1,'Rewrite the weekly recap.','needs_attention',0,0)")

    def tearDown(self):
        self.tmp.cleanup()

    def test_saved_message_holds_owners_words_only(self):
        saved = self.runtime.compose_turn(message("还有什么有意思的主题"), nudge=False)
        self.assertIn("还有什么有意思的主题", saved)
        self.assertNotIn("Background work", saved)
        self.assertNotIn("Alex wrote this", saved)

    def test_request_carries_the_notes_on_the_current_message_only(self):
        rt = self.runtime
        history = [{"role": "user", "content": "seed"}, {"role": "assistant", "content": "ok"}]
        history.append({"role": "user", "content": rt.compose_turn(message("第一个问题", 1791813000), nudge=False)})
        history.append({"role": "assistant", "content": "答复"})
        history.append({"role": "user", "content": rt.compose_turn(message("第二个问题"), nudge=False)})
        before = [dict(m) for m in history]
        wire = rt.trim_history(history, 1)
        self.assertIn('"recorded_state": "needs_attention"', wire[-1]["content"])          # true now, so it is sent now
        self.assertIn("Alex wrote this in Chinese", wire[-1]["content"])
        self.assertNotIn("Background work", wire[2]["content"])             # the earlier turn carries nothing
        self.assertEqual(history, before)                                   # saved history untouched

    def test_cancelled_work_disappears_from_the_next_request(self):
        rt = self.runtime
        history = [{"role": "user", "content": "seed"}, {"role": "assistant", "content": "ok"},
                   {"role": "user", "content": rt.compose_turn(message("第一个问题", 1791813000), nudge=False)}]
        self.assertIn('"task_id": 143', rt.trim_history(history, 1)[-1]["content"])
        with rt._background.db() as db:
            db.execute("UPDATE tasks SET status='cancelled' WHERE id=143")
        history += [{"role": "assistant", "content": "答复"},
                    {"role": "user", "content": rt.compose_turn(message("第二个问题"), nudge=False)}]
        wire = rt.trim_history(history, 1)
        self.assertFalse(any('"task_id": 143' in str(m.get("content")) for m in wire))

    def test_research_hand_off_copies_owners_words(self):
        rt = self.runtime
        rt.conversations[1] = [{"role": "user", "content": rt.compose_turn(message("还有什么有意思的主题"), nudge=False)}]
        started = []
        rt._background.start = lambda chat, request, context: started.append(request) or {"id": 148, "request": request}
        rt._background.label = lambda task: "research"
        rt._execute_tool_unguarded("list_research_reports", {"date_from": "2026-10-05"}, 1)
        self.assertEqual(len(started), 1)
        self.assertIn("还有什么有意思的主题", started[0])
        self.assertNotIn('"task_id": 143', started[0])
        self.assertNotIn("Alex wrote this", started[0])


if __name__ == "__main__":
    unittest.main()
