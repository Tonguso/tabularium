from agent_harness import put
"""Trials: two reruns of a past task, current skill against the proposed edit, before Alex approves (2026-10-20).

Why: a lesson went from 'proposed' to 'applied' on the strength of Cato's argument. Now approval normally follows a
trial, so Alex judges two real outputs; the trial changes nothing and sends nothing but the comparison."""
import json, os, sqlite3, tempfile, unittest
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
from agent_core import learning, trials
from agent_core.outcome import Refused
import background_runtime as rt

SKILL = '---\nname: basket\nmetadata:\n  version: "1.8.5"\n---\n\n## Membership\n- Read the archives for each name.\n'


class Trials(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name).resolve()
        (self.root / "task_state").mkdir()
        with closing(sqlite3.connect(self.root / "task_state" / "conversation_recall.sqlite3")) as con, con:
            con.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, chat TEXT, speaker TEXT, text TEXT)")
            con.execute("INSERT INTO messages VALUES (7, '1', 'user', 'ok, approve it')")
        self.skill = self.root / "skills" / "basket" / "SKILL.md"
        self.skill.parent.mkdir(parents=True); self.skill.write_text(SKILL, encoding="utf-8")
        self.key = learning.propose(self.root, 1, "Freeze the membership first", "t1", "basket_build")
        learning.propose_change(self.root, 1, self.key, self.skill, "- Read the archives for each name.",
                                "- Freeze the membership first; then read the archives once per name.")
        self.store = rt.TaskStore(self.root)
        self.source = self.store.start(1, "Build a US tower basket workbook", "towers only")["id"]
        put(self.store, self.source, "finished", "done")
        self.env = patch.dict(os.environ, {}, clear=False); self.env.start()

    def tearDown(self):
        self.env.stop()
        os.environ.pop(trials.OVERRIDES_ENV, None); os.environ.pop(trials.TRIAL_DIR_ENV, None)
        self.tmp.cleanup()

    def test_approval_waits_for_a_trial_unless_owner_skips_it(self):
        with self.assertRaisesRegex(ValueError, "Run the trial first"):
            learning.approve(self.root, 1, self.key, 7, "ok, approve it")
        self.assertEqual(learning.approve(self.root, 1, self.key, 7, "ok, approve it", skip_trial=True)["status"], "applied")
        self.assertEqual(learning.read(self.root, 1, self.key)["trial_skipped"]["quote"], "ok, approve it")

    def test_start_makes_two_silent_arms_and_an_edited_copy(self):
        out = trials.start(self.root, 1, self.key, self.source, self.store)
        a, b = out["baseline_task"], out["variant_task"]
        ca = json.loads((self.store.directory(a) / "context.json").read_text(encoding="utf-8"))
        cb = json.loads((self.store.directory(b) / "context.json").read_text(encoding="utf-8"))
        self.assertEqual((ca["trial"]["arm"], cb["trial"]["arm"], ca["notify_telegram"], cb["notify_telegram"]), ("baseline", "variant", False, False))
        self.assertEqual(ca["trial"]["overrides"], {})
        patched = Path(cb["trial"]["overrides"][str(self.skill.resolve())])
        self.assertIn("Freeze the membership first", patched.read_text(encoding="utf-8"))
        self.assertNotIn("Freeze", self.skill.read_text(encoding="utf-8"))                 # the real skill is untouched
        self.assertIn("Trial run", ca["context"]); self.assertIn("towers only", ca["context"])
        self.assertEqual(learning.read(self.root, 1, self.key)["trial"]["variant"], b)
        with self.assertRaisesRegex(ValueError, "already running"):
            trials.start(self.root, 1, self.key, self.source, self.store)

    def test_only_finished_research_or_basket_tasks(self):
        running = self.store.start(1, "Build another basket", "")["id"]
        with self.assertRaisesRegex(ValueError, "finished task"):
            trials.start(self.root, 1, self.key, running, self.store)
        code = self.store.start(1, "Fix the dashboard code", "")["id"]; put(self.store, code, "finished", "")
        with self.assertRaisesRegex(ValueError, "research and basket"):
            trials.start(self.root, 1, self.key, code, self.store)

    def test_the_variant_process_reads_the_edit_and_writes_only_its_folder(self):
        out = trials.start(self.root, 1, self.key, self.source, self.store)
        folder = self.store.directory(out["variant_task"])
        ctx = json.loads((folder / "context.json").read_text(encoding="utf-8"))
        trials.prepare_worker(ctx, folder)
        self.assertIn("Freeze", Path(trials.overridden(self.skill)).read_text(encoding="utf-8"))
        self.assertIsNone(trials.overridden(self.root / "skills" / "other.md"))
        self.assertIsNone(trials.write_refusal(str(folder / "trial_output" / "basket.xlsx")))
        self.assertIsInstance(trials.write_refusal(r"D:\Baskets\US Tower Infrastructure 2026-10-11.xlsx"), Refused)
        runtime = NS(_all_tools=lambda p: [{"name": n} for n in ("read_file", "send_email", "delegate", "web_search", "lesson")])
        trials.scope(runtime)
        self.assertEqual(runtime._tool_scope, {"read_file", "web_search"})

    def test_the_second_arm_to_finish_sends_the_comparison(self):
        out = trials.start(self.root, 1, self.key, self.source, self.store)
        a, b = out["baseline_task"], out["variant_task"]
        prompts = []

        @contextmanager
        def scope(names):
            self.assertEqual(names, set()); yield
        runtime = NS(load_memory_context=lambda: [], _ts=lambda: "[ts]", tool_scope=scope,
                     run_turn=lambda messages, chat: prompts.append(messages[-1]["content"]) or "1. 差异…\n2. …\n3. …\n建议: approve")
        agent = NS(runtime=runtime)
        ctx = lambda i: json.loads((self.store.directory(i) / "context.json").read_text(encoding="utf-8"))
        self.assertEqual(trials.after_arm(agent, self.store, a, ctx(a), "baseline answer"), "baseline answer")   # first: silent
        report = trials.after_arm(agent, self.store, b, ctx(b), "variant answer")
        self.assertIn("【技能修改试验】", report); self.assertIn("建议: approve", report)
        self.assertIn("baseline answer", prompts[0]); self.assertIn("variant answer", prompts[0])
        self.assertIn("Freeze the membership first", prompts[0])
        self.assertTrue(ctx(b)["notify_telegram"]); self.assertFalse(ctx(a)["notify_telegram"])
        self.assertTrue(learning.read(self.root, 1, self.key)["trial"]["done"])
        self.assertEqual(learning.approve(self.root, 1, self.key, 7, "ok, approve it")["status"], "applied")   # no skip needed now

    def test_an_ordinary_task_is_untouched(self):
        self.assertEqual(trials.after_arm(None, self.store, self.source, {"context": "x"}, "answer"), "answer")
        trials.prepare_worker({"context": "x"}, self.store.directory(self.source))
        self.assertIsNone(os.environ.get(trials.TRIAL_DIR_ENV))


TRIAL_STUB = """from pathlib import Path
from types import SimpleNamespace as NS
from contextlib import contextmanager
import json, os
from background_runtime import TaskStore
from agent_core.trials import overridden
AGENT_DIR = Path(__file__).resolve().parent
store = TaskStore(AGENT_DIR)
ident = int(os.environ["CATO_BACKGROUND_TASK"])
RUNTIME_DIR = store.directory(ident)
_inbox = NS(local=NS())
SYSTEM_PROMPT = "fixture"
conversations = {}
class RunawayStop(RuntimeError):
    pass
def load_model_prefs(chat): pass
def _conversation_path(chat): return RUNTIME_DIR / f"conversation_{chat}.json"
def load_conversation(chat): return []
def save_conversation(chat): _conversation_path(chat).write_text(json.dumps(conversations.get(chat, [])))
def skill_text():
    real = AGENT_DIR / "skills" / "basket" / "SKILL.md"
    return Path(overridden(real) or real).read_text(encoding="utf-8")
def run_turn(messages, chat):
    return "answer with " + ("frozen membership" if "Freeze" in skill_text() else "the archives read per name")
@contextmanager
def _scope(names):
    yield
runtime = NS(SYSTEM_PROMPT=SYSTEM_PROMPT, BACKGROUND_TASK_ID=str(ident), _tool_scope=None,
             _all_tools=lambda p: [{"name": n} for n in ("read_file", "send_email", "web_search")],
             tool_scope=_scope, load_memory_context=lambda: [], _ts=lambda: "[ts]",
             run_turn=lambda messages, chat: "1. B froze the membership. 2. B. 3. none. 建议: approve | seen: "
                                             + str("frozen membership" in messages[-1]["content"]))
"""


class RealWorkers(unittest.TestCase):
    def test_two_worker_processes_run_the_trial_and_one_message_reaches_owner(self):
        import time
        with tempfile.TemporaryDirectory() as tmp:
            root = (Path(tmp) / "cato").resolve(); (root / "task_state").mkdir(parents=True)
            (root / "telegram.py").write_text(TRIAL_STUB, encoding="utf-8")
            skill = root / "skills" / "basket" / "SKILL.md"; skill.parent.mkdir(parents=True); skill.write_text(SKILL, encoding="utf-8")
            key = learning.propose(root, 1, "Freeze the membership first", "t1", "basket_build")
            learning.propose_change(root, 1, key, skill, "- Read the archives for each name.", "- Freeze the membership first.")
            store = rt.TaskStore(root)
            source = store.start(1, "Build a US tower basket workbook", "towers")["id"]; put(store, source, "finished", "")
            put(store, source, "finished", ""); store.tick(lambda c, t: None)                    # the source's own notice
            out = trials.start(root, 1, key, source, store)
            notices = []
            try:
                deadline = time.monotonic() + 40
                while time.monotonic() < deadline:
                    store.tick(lambda chat, text: notices.append(text))
                    if notices and all(store.get(i)["status"] not in ("queued", "starting", "running") for i in (out["baseline_task"], out["variant_task"])):
                        break
                    time.sleep(0.05)
                logs = "".join((store.directory(i) / "worker.log").read_text(encoding="utf-8", errors="replace")
                               for i in (out["baseline_task"], out["variant_task"]) if (store.directory(i) / "worker.log").exists())
                self.assertEqual(len(notices), 1, logs)
                self.assertIn("【技能修改试验】", notices[0]); self.assertIn("seen: True", notices[0])
                a = (store.directory(out["baseline_task"]) / "answer.txt").read_text(encoding="utf-8")
                b = (store.directory(out["variant_task"]) / "answer.txt").read_text(encoding="utf-8")
                self.assertEqual((a, b), ("answer with the archives read per name", "answer with frozen membership"))
                self.assertNotIn("Freeze", skill.read_text(encoding="utf-8"))                   # nothing changed
                self.assertTrue(learning.read(root, 1, key)["trial"]["done"])
            finally:
                for proc in store.processes.values():
                    if proc.poll() is None:
                        proc.terminate()
                    proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
