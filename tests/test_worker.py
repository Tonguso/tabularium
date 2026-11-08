"""A real background worker process (python background_runtime.py --worker <id> --agent <dir>) with a scripted agent:
a looping agent is stopped with the runtime's own words and the task needs attention (2026-10-07, 2026-10-12)."""
import sys, tempfile, time, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
from tabularium.records import background_runtime
from agent_harness import put

STUB = '''from pathlib import Path
from types import SimpleNamespace as NS
import json, os
from tabularium.records.background_runtime import TaskStore
AGENT_DIR = Path(__file__).resolve().parent
store = TaskStore(AGENT_DIR)
ident = int(os.environ["CATO_BACKGROUND_TASK"])
RUNTIME_DIR = store.directory(ident)
_inbox = NS(local=NS())
SYSTEM_PROMPT = "fixture"
runtime = NS(SYSTEM_PROMPT=SYSTEM_PROMPT)
conversations = {}
class RunawayStop(RuntimeError):
    pass
def load_model_prefs(chat): pass
def _conversation_path(chat): return RUNTIME_DIR / f"conversation_{chat}.json"
def load_conversation(chat): return []
def save_conversation(chat): _conversation_path(chat).write_text(json.dumps(conversations[chat]))
def run_turn(messages, chat):
    messages.append({"role": "assistant", "content": "notes to self: try review_output again with the same input"})
    raise RunawayStop("5 consecutive tool calls failed or were refused; the last was review_output")
'''


class LoopingWorker(unittest.TestCase):
    def test_a_looping_agent_is_stopped_in_the_runtimes_words(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "cato"; root.mkdir()
            (root / "agent.py").write_text(STUB, encoding="utf-8")
            store = background_runtime.TaskStore(root)
            task = store.start(1, "Review the workbook")
            notices = []
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    store.tick(lambda chat, text: notices.append(text))
                    if store.get(task["id"])["status"] not in ("queued", "starting", "running") and notices:
                        break
                    time.sleep(0.05)
                log = (store.directory(task["id"]) / "worker.log")
                self.assertEqual(store.get(task["id"])["status"], "needs_attention",
                                 log.read_text(encoding="utf-8", errors="replace") if log.exists() else "")
                result = (store.directory(task["id"]) / "result.txt").read_text(encoding="utf-8")
                self.assertTrue(result.startswith("I stopped this background task"))
                self.assertNotIn("notes to self", result)              # the model's last text never reaches Alex
                self.assertEqual(notices, [result])
            finally:
                for proc in store.processes.values():
                    if proc.poll() is None:
                        proc.terminate()
                    proc.wait(timeout=5)


class Notification(unittest.TestCase):
    def test_a_stopped_task_reports_what_it_saved(self):
        """A capped or needs-attention task sends its saved result, not a generic 'stopped' line (2026-10-07)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "cato"; root.mkdir()
            store = background_runtime.TaskStore(root)
            for status in ("capped", "needs_attention"):
                task = store.start(1, "Build the basket")
                (store.directory(task["id"]) / "result.txt").write_text(f"Partial basket ({status})", encoding="utf-8")
                put(store, task["id"], status)
            sent = []
            store.tick(lambda chat, text: sent.append(text))                # tick sends any unreported outcome
            self.assertEqual(sorted(sent), ["Partial basket (capped)", "Partial basket (needs_attention)"])


if __name__ == "__main__":
    unittest.main()
