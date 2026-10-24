import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from datetime import datetime
import unittest
from unittest.mock import patch

from background_runtime import TaskStore
from agent_core.roster import ROLES
from agent_core.reply_envelope import Reply
from agent_core.task_decisions import answer_record, verify_reply
from agent_core.worker_lease import WorkerLease
from tests.agent_harness import make_agent, put


class DurableDecisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)   # a worker may still hold jobs.sqlite3 on a slow machine (CI, 2026-10-24)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = TaskStore(self.root)
        self.task = self.store.start(1, "Produce a report after choosing a setting", "")["id"]
        put(self.store, self.task, "running")

    def source(self, speaker="user"):
        db = sqlite3.connect(self.root / "task_state/conversation_recall.sqlite3")
        try:
            db.execute("CREATE TABLE messages(id INTEGER,chat TEXT,speaker TEXT,text TEXT,source TEXT,said_at TEXT)")
            db.execute("INSERT INTO messages VALUES (7,'1',?,'Use option B',?,?)",
                       (speaker, str(self.root / "conversation_1.json"), datetime.now().isoformat()))
            db.commit()
        finally:
            db.close()

    def test_all_roles_wait_answer_restart_deliver_without_claiming_unverified_completion(self):
        self.source()
        for role in ROLES.values():
            self.store.role = role
            ident = self.store.start(1, role.name + " report", "")["id"]
            put(self.store, ident, "running")
            result = Reply("Choose A or B", task_outcome="waiting", question="Choose A or B")
            self.assertEqual(self.store.finish(ident, result), "needs_answer")
            restarted = TaskStore(self.root, role)
            decision = restarted.decisions(1, ident)[0]
            self.assertIn("Choose A or B", restarted.live_context(1))
            self.assertEqual(restarted.live(1, ident)["state"], "waiting")
            self.assertRaises(ValueError, restarted.control, 1, ident, "resume")
            resolved = restarted.answer(1, decision["id"], "B", 7, "Use option B")
            restarted.answer(1, decision["id"], "B", 7, "Use option B")
            self.assertEqual(len(restarted.pending_controls(ident)), 1)
            # A worker exiting after the answer cannot mark it finished or block the continuation.
            self.assertEqual(restarted.finish(ident, result), "queued")
            restarted.acknowledge([resolved["control"]])
            self.assertEqual(restarted.pending_controls(ident), [])
            self.assertIn("Answer: B", restarted.contract(ident))
            put(restarted, ident, "running")
            self.assertEqual(restarted.finish(ident, Reply("Report delivered", task_outcome="completed")), "needs_attention")
            self.assertFalse(restarted.decisions(1, ident))

    def test_plain_return_and_blocked_completion_are_not_finished(self):
        self.assertEqual(self.store.finish(self.task, "Here is a partial result"), "needs_attention")
        folder = self.store.directory(self.task)
        (folder / "checkpoint.json").write_text(json.dumps({"blocker": "Need a user decision"}))
        put(self.store, self.task, "running")                    # a worker finishes only from running (lifecycle)
        self.assertEqual(self.store.finish(self.task, Reply("Done", task_outcome="completed")), "needs_attention")

    def test_assistant_text_cannot_answer_and_cancel_keeps_audit(self):
        self.source("assistant")
        d = self.store.ask(1, self.task, "Choose A or B")
        self.assertRaises(ValueError, self.store.answer, 1, d["id"], "B", 7, "Use option B")
        self.store.control(1, self.task, "cancel")
        self.assertFalse(self.store.decisions(1))
        self.assertRaises(ValueError, answer_record, self.store, 1, d["id"], "B", "owner")

    def test_pending_question_survives_more_than_ten_new_tasks(self):
        self.store.ask(1, self.task, "Old unresolved choice")
        for _ in range(12):
            ident = self.store.start(1, "new", "")["id"]
            put(self.store, ident, "finished")
        self.assertIn("Old unresolved choice", TaskStore(self.root).live_context(1))

    def test_waiting_delegate_reports_through_owner_not_duplicate_chat(self):
        self.store.role = ROLES["callimachus"]
        db = sqlite3.connect(self.store.database)
        try:
            db.execute("CREATE TABLE research_requests(task INTEGER)")
            db.execute("INSERT INTO research_requests VALUES (?)", (self.task,))
            db.commit()
        finally:
            db.close()
        self.store.ask(1, self.task, "A or B?")
        self.store.finish(self.task, Reply("Choose A or B", task_outcome="waiting"))
        notices = []
        self.store.tick(lambda chat, text: notices.append(text))
        self.assertEqual(notices, [])
        self.assertEqual(self.store.open_question(self.task), "A or B?")

    def test_running_requires_lease_pid_and_current_heartbeat(self):
        self.assertEqual(self.store.live(1, self.task)["state"], "stopped")
        lease = WorkerLease(self.store.directory(self.task) / "worker.lock")
        self.assertTrue(lease.acquire())
        self.addCleanup(lease.close)
        db = sqlite3.connect(self.store.database)
        try:
            db.execute("UPDATE tasks SET pid=? WHERE id=?", (os.getpid(), self.task))
            db.commit()
        finally:
            db.close()
        path = self.store.directory(self.task) / "task_state/health.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"pid": os.getpid(), "deadline": time.time() + 60}))
        self.assertEqual(self.store.live(1, self.task)["state"], "running")
        path.write_text(json.dumps({"pid": os.getpid(), "deadline": time.time() - 1}))
        self.assertEqual(self.store.live(1, self.task)["state"], "stopped")

    def test_question_cannot_be_overwritten_or_stale_worker_finish(self):
        d = self.store.ask(1, self.task, "A or B?")
        self.assertEqual(self.store.ask(1, self.task, "A or B?")["id"], d["id"])
        self.assertRaises(ValueError, self.store.ask, 1, self.task, "C or D?")
        self.store.worker_attempt = "stale"
        self.assertRaises(ValueError, self.store.finish, self.task, Reply("Done", task_outcome="completed"))


class AgentDecisionFlowTests(unittest.TestCase):
    def test_new_waiting_claim_is_recorded_before_verification(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            agent = make_agent("cato", tmp.name, background=True)
            # A worker's own declared question is recorded without a second model call (2026-10-20).
            out = verify_reply(agent, [], 1, Reply("Choose A or B", task_outcome="waiting", question="A or B?", decision_task_id=1,
                                                   status_claims=[{"task_id": 1, "state": "waiting"}]))
            self.assertTrue(out.complete)
            self.assertEqual(agent._background.get(1)["status"], "running")
            self.assertEqual(agent._background.finish(1, out), "needs_answer")
            self.assertEqual(agent._background.get(1)["status"], "needs_answer")
            self.assertEqual(agent._background.decisions(1)[0]["question"], "A or B?")
        finally:
            tmp.cleanup()

    def test_all_agents_tools_context_and_omitted_claim_audit(self):
        for name in ROLES:
            tmp = tempfile.TemporaryDirectory()
            try:
                agent = make_agent(name, tmp.name)
                self.assertIn("task_decision", [t["name"] for t in agent.get_tools("anthropic")])
                messages = [{"role": "user", "content": "Is it running?"},
                            {"role": "assistant", "content": "Yes, still working"}]
                # Independent extraction catches a claim the original draft omitted.
                fake = patch.object(agent._routing, "run", return_value=Reply(
                    "audited", status_claims=[{"task_id": 1, "state": "running"}]))
                fake.start()
                try:
                    result = verify_reply(agent, messages, 1, Reply("Yes, still working"))
                finally:
                    fake.stop()
                # The answer goes out as written; the disagreement is logged, never appended to Alex's chat
                # (2026-10-20 22:08-22:09: the appended "状态更正" line, from stale records, is what he swore at).
                self.assertEqual(str(result), "Yes, still working")
                self.assertEqual(messages[-1]["content"], result)
                self.assertIsNone(agent._tool_scope)
                question = agent.execute_tool("task_decision", {"action": "ask", "task_id": 1, "question": "A or B?"}, 1)
                self.assertIn("pending", question)
                original = [{"role": "user", "content": "hello"}]
                wire = agent.trim_history(original, 1)
                self.assertIn("A or B?", str(wire))
                self.assertEqual(original, [{"role": "user", "content": "hello"}])
            finally:
                tmp.cleanup()


class KeepTheAnswer(unittest.TestCase):
    """2026-10-20: task 234 reverse-engineered a client basket and ended on one question for Alex. The audit read the
    worker's own task as 'waiting' while it was still running and replaced the whole answer with a status line."""

    def test_a_workers_claim_about_its_own_task_is_its_outcome_not_a_mismatch(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            agent = make_agent("cato", tmp.name, background=True)
            draft = "一、结论：核心是 A&D 二线供应商。需要您确认：FTI 是不是 FTAI 的笔误？"
            fake = patch.object(agent._routing, "run", return_value=Reply(
                "audited", task_outcome="waiting", question="FTI 是不是 FTAI 的笔误？", decision_task_id=1,
                status_claims=[{"task_id": 1, "state": "waiting"}, {"task_id": int(agent.BACKGROUND_TASK_ID), "state": "waiting"}]))
            fake.start()
            try:
                out = verify_reply(agent, [], 1, Reply(draft))
            finally:
                fake.stop()
            self.assertTrue(out.startswith("一、结论"))
            self.assertNotIn("状态更正", out)
        finally:
            tmp.cleanup()

    def test_a_wrong_status_is_logged_and_the_reply_is_untouched(self):
        # 2026-10-20 22:08: "状态更正：任务 241 在等您决定；任务 245 没有在运行。这都是啥？"
        tmp = tempfile.TemporaryDirectory()
        try:
            agent = make_agent("cato", tmp.name)
            # The declared claim is checked against the records; no second model reads the reply (2026-10-21).
            with patch.object(agent._routing, "run") as model:
                out = verify_reply(agent, [], 1, Reply("反推已经做完了，结论如下……",
                                                       status_claims=[{"task_id": 1, "state": "finished"}]))
            model.assert_not_called()
            self.assertEqual(str(out), "反推已经做完了，结论如下……")
            with agent._inbox.db() as db:
                logged = db.execute("SELECT model FROM usage_events WHERE kind='status_mismatch'").fetchall()
            self.assertEqual(len(logged), 1)
            self.assertTrue(logged[0][0].startswith("1:"))
        finally:
            tmp.cleanup()

    def test_a_failed_check_still_delivers_the_answer(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            agent = make_agent("cato", tmp.name)
            fake = patch.object(agent._routing, "run", side_effect=RuntimeError("route down"))
            fake.start()
            try:
                out = verify_reply(agent, [], 1, Reply("Here is the basket analysis."))
            finally:
                fake.stop()
            self.assertEqual(str(out), "Here is the basket analysis.")
        finally:
            tmp.cleanup()


class WorkerProcessDecisionTest(unittest.TestCase):
    def test_real_process_waits_then_resumes_once_and_finishes(self):
        tmp = tempfile.TemporaryDirectory()
        store = None
        try:
            root = Path(tmp.name)
            (root / "telegram.py").write_text(WORKER_STUB, encoding="utf-8")
            store = TaskStore(root)
            task = store.start(1, "Choose a setting then produce the report", "")
            notices = []
            def until(state):
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    store.tick(lambda chat, text: notices.append(text))
                    if store.get(task["id"])["status"] == state and not store.lease_busy(task["id"]) and notices:
                        return
                    time.sleep(.03)
                self.fail((store.directory(task["id"]) / "worker.log").read_text(errors="replace"))
            until("needs_answer")
            self.assertIn("Choose A or B", notices[0])
            d = store.decisions(1)[0]
            restarted = TaskStore(root)
            self.assertEqual(restarted.decisions(1)[0]["id"], d["id"])
            answer_record(restarted, 1, d["id"], "B", "verified test owner")
            notices.clear()
            until("finished")
            self.assertEqual(store.pending_controls(task["id"]), [])
            self.assertIn("Report with B delivered", notices[0])
            for _ in range(3):
                store.tick(lambda chat, text: notices.append(text))
            self.assertEqual(len(notices), 1)
        finally:
            if store:
                for proc in store.processes.values():
                    if proc.poll() is None:
                        proc.terminate()
                        proc.wait(timeout=5)
            tmp.cleanup()


WORKER_STUB = '''
from pathlib import Path
from types import SimpleNamespace as NS
import json, os
from background_runtime import TaskStore
from agent_core.reply_envelope import Reply
from agent_core import task_decisions
task_decisions.audit=lambda agent,chat,draft,evidence: draft
AGENT_DIR=Path(__file__).resolve().parent
ident=int(os.environ["CATO_BACKGROUND_TASK"])
store=TaskStore(AGENT_DIR)
RUNTIME_DIR=store.directory(ident)
_inbox=NS(local=NS())
SYSTEM_PROMPT="fixture"
runtime=NS(SYSTEM_PROMPT=SYSTEM_PROMPT, BACKGROUND_TASK_ID=str(ident))
conversations={}
class RunawayStop(RuntimeError): pass
def load_model_prefs(chat): pass
def _conversation_path(chat): return RUNTIME_DIR / f"conversation_{chat}.json"
def load_conversation(chat):
    p=_conversation_path(chat)
    return json.loads(p.read_text()) if p.exists() else []
def save_conversation(chat): _conversation_path(chat).write_text(json.dumps(conversations[chat]))
runtime.save_conversation=save_conversation
last=[]
def run_turn(messages,chat):
    if last:
        return last[-1]
    updates=store.pending_controls(ident)
    if updates:
        assert len(updates)==1 and "Answer: B" in updates[0]["text"]
        messages.append({"role":"user","content":updates[0]["text"]})
        save_conversation(chat)
        store.acknowledge([updates[0]["id"]])
        artifact=RUNTIME_DIR / "report.txt"
        artifact.write_text("Report with B delivered")
        store.checkpoint(chat, ident, "Saved report", "", [], completion_items=[{
            "item": "Report with B", "check": "Read saved report", "verification": {
                "kind": "contains", "paths": [str(artifact)], "text": "Report with B delivered"}}])
        store.checkpoint(chat, ident, "Recorded answer", "", [], completion_items=[{
            "item": "Use decision B", "source": updates[0]["id"], "check": "Read saved decision", "verification": {
                "kind": "contains", "paths": [str(artifact)], "text": "Report with B delivered"}}])
        from agent_core import verification
        checker=NS(_background=store, BACKGROUND_TASK_ID=str(ident), config=NS(background_refusals=False),
                   get_tools=lambda provider: [{"name": "read_file"}],
                   _execute_tool_unguarded=lambda name, args, chat: Path(args["path"]).read_text())
        verification.execute(checker, chat, ident, 1)
        verification.execute(checker, chat, ident, 2)
        out=Reply("Report with B delivered", task_outcome="completed")
    else:
        out=Reply("Choose A or B", task_outcome="waiting",question="Choose A or B")
    last.append(out)
    messages.append({"role":"assistant","content":out})
    return out
'''


if __name__ == "__main__":
    unittest.main()
