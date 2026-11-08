"""A background task's brief and added instructions stay in view and are checked before delivery (2026-10-12).

Task 144 (2026-10-11) received Alex's 'also build a carriers basket' at message 38; the history window dropped the
brief at message 52 and the instruction at message 73, and the worker delivered the tower basket only."""
import sys, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
from tabularium.records import background_runtime as rt
from tabularium.core import engine
def store_with_task(root):
    store = rt.TaskStore(root)
    with store.db() as db:
        db.execute("INSERT INTO tasks(id,chat,request,status,created,updated) VALUES (7,1,'Build a US tower basket.','running',0,0)")
        db.execute("INSERT INTO controls(task,text,applied) VALUES (7,'Also build a US carriers basket after the towers.',1)")
        db.execute("INSERT INTO controls(task,text,applied) VALUES (7,'Not yet received by the worker.',0)")
    return store


class Contract(unittest.TestCase):
    def test_contract_has_brief_and_received_instructions_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = store_with_task(Path(tmp)).contract(7)
            self.assertIn("Build a US tower basket.", text)
            self.assertIn("Also build a US carriers basket", text)
            self.assertNotIn("Not yet received", text)          # pending updates arrive through the boundary first

    def test_trimmed_window_carries_the_contract_and_history_is_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = engine.AgentRuntime()
            runtime.AGENT_DIR = Path(tmp)
            runtime._background = store_with_task(Path(tmp))
            runtime.BACKGROUND_TASK_ID = "7"
            runtime._load_core_memory = lambda: ""      # no memory file in this fixture
            runtime.WINDOW_LOW, runtime.WINDOW_HIGH = 30, 50
            messages = [{"role": "user", "content": "core memory"}, {"role": "assistant", "content": "ready"},
                        {"role": "user", "content": "Build a US tower basket."}]
            for i in range(120):
                messages.append({"role": "assistant" if i % 2 == 0 else "user", "content": f"step {i}"})
            window = runtime.trim_history(messages, 1)
            self.assertLess(len(window), len(messages))
            self.assertNotIn("Build a US tower basket.", [m["content"] for m in window[2:]])   # the brief scrolled out...
            self.assertIn("Also build a US carriers basket", window[0]["content"])           # ...but the contract is pinned
            self.assertEqual(messages[0]["content"], "core memory")                          # canonical history unchanged

    def test_foreground_window_is_not_changed(self):
        runtime = engine.AgentRuntime()
        runtime.AGENT_DIR = Path(tempfile.gettempdir())
        runtime.BACKGROUND_TASK_ID = ""
        from types import SimpleNamespace
        runtime._inbox = SimpleNamespace(turn_sources=lambda chat: [])
        runtime._load_core_memory = lambda: ""
        runtime.WINDOW_LOW, runtime.WINDOW_HIGH = 30, 50
        messages = [{"role": "user", "content": "core"}, {"role": "assistant", "content": "ready"}]
        messages += [{"role": "user" if i % 2 == 0 else "assistant", "content": str(i)} for i in range(100)]
        self.assertEqual(runtime.trim_history(messages, 2)[0]["content"], "core")


if __name__ == "__main__":
    unittest.main()
