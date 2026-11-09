"""One task lifecycle and one record for everything Alex is asked (2026-10-21).

Reproduces the failures the review found: a question about a finished task silently set it back to needs_answer; a
confirmation waited 60 seconds in memory, so a late yes counted as no and a restart forgot the question; and an
answer could reach a decision only by the agent searching history and reconstructing what Alex meant."""
import contextlib, io, json, tempfile, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_harness import make_agent, put
from tabularium.records import lifecycle
from tabularium.records import task_decisions
from tabularium.records.lifecycle import InvalidTransition
from tabularium.core.outcome import Refused
from tabularium.core.reply_envelope import Reply
from tabularium.records.background_runtime import TaskStore
from tabularium.channels.owner_telegram import OWNER_CHAT_ID
CHAT = OWNER_CHAT_ID


def message(text, reply_to=None, ident=None):
    return SimpleNamespace(chat=SimpleNamespace(id=CHAT), text=text, message_id=ident or int(time.time() * 1000) % 10**9,
                           date=int(time.time()), reply_to_message=SimpleNamespace(message_id=reply_to) if reply_to else None)


class Transitions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = TaskStore(self.tmp.name)
        self.task = self.store.start(CHAT, "Build the report", "")["id"]

    def test_a_question_about_a_finished_task_is_refused_and_the_task_stays_finished(self):
        put(self.store, self.task, "finished")
        with self.assertRaisesRegex(ValueError, "finished"):
            self.store.ask(CHAT, self.task, "Want a follow-up?")
        self.assertEqual(self.store.get(self.task)["status"], "finished")
        self.assertEqual(self.store.decisions(CHAT), [])

    def test_reopening_a_finished_task_is_its_own_operation(self):
        put(self.store, self.task, "finished")
        self.store.control(CHAT, self.task, "resume", "Add the Japan names")
        task = self.store.get(self.task)
        self.assertEqual(task["status"], "queued")
        self.assertTrue(task["note"].startswith("Reopened"))

    def test_moves_outside_the_table_are_refused_and_write_nothing(self):
        put(self.store, self.task, "finished")
        for op, to in (("run", "running"), ("ask", "needs_answer"), ("finish", "finished"), ("cancel", "cancelled")):
            with self.assertRaises(InvalidTransition):
                self.store.move(self.task, op, to)
        with self.assertRaises(InvalidTransition):
            self.store.move(self.task, "stop", "finished")          # stop can never lead to finished
        self.assertEqual(self.store.get(self.task)["status"], "finished")

    def test_a_task_that_asked_a_question_can_still_park_on_a_delegate(self):
        # 2026-10-22, task 264: the worker's draft recorded a question for Alex (needs_answer) and the delivery gate
        # then found Vit's request in flight; park from needs_answer raised InvalidTransition and the worker crashed.
        put(self.store, self.task, "running")
        self.store.ask(CHAT, self.task, "Which font size?")
        self.assertEqual(self.store.get(self.task)["status"], "needs_answer")
        self.store.add_delegation(self.task, "vit", "req-1", "Tiro fixes", "owner_decision")
        self.store.move(self.task, "park", lifecycle.WAITING)
        self.assertEqual(self.store.get(self.task)["status"], lifecycle.WAITING)
        self.assertEqual(len(self.store.decisions(CHAT, self.task)), 1)      # the question is still pending

    def test_an_answer_while_parked_is_recorded_and_the_task_keeps_waiting(self):
        put(self.store, self.task, "running")
        decision = self.store.ask(CHAT, self.task, "Which font size?")
        self.store.add_delegation(self.task, "vit", "req-1", "Tiro fixes", "owner_decision")
        self.store.move(self.task, "park", lifecycle.WAITING)
        from tabularium.records.task_decisions import answer_record
        answer_record(self.store, CHAT, decision["id"], "14 point", '{"relay": "test"}')
        self.assertEqual(self.store.get(self.task)["status"], lifecycle.WAITING)
        self.assertEqual(self.store.decisions(CHAT, self.task), [])
        self.assertTrue(any("14 point" in c["text"] for c in self.store.pending_controls(self.task)))

    def test_the_monitor_form_does_nothing_when_the_task_has_moved_on(self):
        put(self.store, self.task, "finished")
        self.assertFalse(self.store.move(self.task, "stop", "interrupted", "dead worker", quiet=True))
        self.assertEqual(self.store.get(self.task)["status"], "finished")

    def test_every_status_the_code_reaches_is_in_the_table(self):
        reachable = {t for _, targets in lifecycle.OPS.values() for t in targets} | {"queued"}
        self.assertTrue({"finished", "needs_answer", "needs_attention", "interrupted", "cancelled", "cancelling",
                         "running", "starting", lifecycle.WAITING} <= reachable)


class Agent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "coordinator"; self.root.mkdir()
        self.next_id = 500

    def agent(self):
        with contextlib.redirect_stdout(io.StringIO()):
            agent = make_agent("coordinator", self.root)
        agent.BACKGROUND_TASK_ID = ""
        agent.bot = Mock()
        def send(chat, text, **kw):
            self.next_id += 1
            return SimpleNamespace(message_id=self.next_id)
        agent.bot.send_message.side_effect = send
        return agent

    def test_a_parked_command_survives_restart_and_a_late_yes_runs_it_exactly_once(self):
        first = self.agent()
        first.get_auto_mode = lambda chat: False
        out = first.execute_tool("execute_command", {"command": "Remove-Item -Recurse D:\\Baskets\\old"}, CHAT)
        self.assertIsInstance(out, Refused)
        parked = task_decisions.pending(first._background, CHAT, kind="action")
        self.assertEqual(len(parked), 1)
        asked = json.loads(parked[0]["message_ids"])
        self.assertEqual(json.loads(parked[0]["action"]), {"tool": "execute_command",
                                                           "inputs": {"command": "Remove-Item -Recurse D:\\Baskets\\old"}})
        # The agent restarts. An hour later Alex replies yes to the question.
        second = self.agent()
        second.get_auto_mode = lambda chat: False
        ran = []
        class Proc:
            pid, returncode = 1, 0
            def communicate(self, timeout=None):
                return b"removed", b""
        def popen(argv, **kw):
            ran.append(argv[-1]); return Proc()
        with patch("tabularium.core.agent_app.subprocess.Popen", side_effect=popen):
            self.assertEqual(second._settle_by_reply(message("yes", reply_to=asked[0])), (True, ""))
            self.assertEqual(second._settle_by_reply(message("yes", reply_to=asked[0])), (False, ""))   # settled
        self.assertEqual(ran, ["Remove-Item -Recurse D:\\Baskets\\old"])
        self.assertEqual(task_decisions.pending(second._background, CHAT, kind="action"), [])

    def test_a_plain_yes_is_ambiguous_when_two_actions_wait(self):
        agent = self.agent()
        for n in (1, 2):
            task_decisions.park_action(agent._background, CHAT, "send_email", {"to": f"{n}@x"}, f"send {n}?", decision_id=0)
        self.assertEqual(agent._settle_by_reply(message("yes")), (False, ""))
        self.assertEqual(len(task_decisions.pending(agent._background, CHAT, kind="action")), 2)

    def test_replying_to_a_question_answers_its_decision_directly(self):
        agent = self.agent()
        task = agent._background.start(CHAT, "Basket build", "")["id"]
        put(agent._background, task, "running")
        decision = agent._background.ask(CHAT, task, "A: 30 names or B: 50 names?")
        ask = Reply("Before I finish: A (30 names) or B (50 names)?")
        ask.decision_id = decision["id"]
        agent.send_message(CHAT, ask)
        asked = json.loads(agent._background.decisions(CHAT)[0]["message_ids"])
        handled, note = agent._settle_by_reply(message("选B", reply_to=asked[0], ident=9001))
        self.assertFalse(handled)                                   # the agent still answers him in a normal turn
        self.assertIn(f"decision #{decision['id']}", note)
        self.assertEqual(len(task_decisions.pending(agent._background, CHAT)), 1)
        self.assertEqual(agent._background.pending_controls(task), [])
        task_decisions.answer_record(agent._background, CHAT, decision['id'], '选B',
                                     json.dumps({'message_id': 9001, 'quote': '选B'}))
        self.assertEqual(task_decisions.pending(agent._background, CHAT), [])
        self.assertEqual(agent._background.get(task)["status"], "queued")
        control = agent._background.pending_controls(task)[0]["text"]
        self.assertIn("Answer: 选B", control)
        self.assertIn('"message_id": 9001', control)

    def test_a_foreground_question_about_a_task_does_not_resend_its_old_result(self):
        """2026-10-22 21:03 and 21:04: each time Cato asked Alex something about task 264 from the foreground, the
        monitor re-sent the task's result from 19:41 ("Portable Tiro is ready to use"), because the ask move reset
        notified to 0. The foreground reply already carries the question; the monitor has nothing to send."""
        agent = self.agent()
        store = agent._background
        with store.db() as db:
            db.execute("UPDATE tasks SET status='finished', notified=1")          # the harness's own self-test task
        task = store.start(CHAT, "Fix Tiro", "")["id"]
        put(store, task, "needs_attention")
        (store.directory(task) / "result.txt").write_text("Portable Tiro is ready to use.", encoding="utf-8")
        with store.db() as db:
            db.execute("UPDATE tasks SET notified=1 WHERE id=?", (task,))      # delivered at 19:41
        store.ask(CHAT, task, "修复要不要排到另一个任务前面先做？")
        self.assertEqual(store.get(task)["status"], "needs_answer")
        send = Mock()
        store.tick(send)
        send.assert_not_called()
        self.assertEqual(store.get(task)["notified"], 1)

    def test_optional_follow_up_explicitly_uses_a_conversation_question(self):
        agent = self.agent()
        task = agent._background.start(CHAT, "Report", "")["id"]
        put(agent._background, task, "finished")
        draft = Reply("Do you want the Japan names too?", question="Japan names too?", decision_task_id=0)
        out = task_decisions.verify_reply(agent, [], CHAT, draft)
        self.assertEqual(agent._background.get(task)["status"], "finished")
        self.assertEqual([d["task"] for d in agent._background.decisions(CHAT)], [0])
        self.assertTrue(out.decision_id)


if __name__ == "__main__":
    unittest.main()
