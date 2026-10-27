"""Alex's correction starts a diagnosis the agent does itself; he never has to spell the error out (2026-10-20).

22:08 [quote removed] said nothing about the cause. The reply's
`correction` field marks it; the runtime diagnoses what happened and whose fault it is (the agent's method → a
proposed skill edit in the lesson register; the runtime → a report for Claude Code or Codex).

2026-10-22: one evening's argument produced twelve diagnosis tasks, each ending in a yes/no question to Alex
("Why did Cato have so many useless background stuff that needs my attention"). Now a thread of corrections is one
diagnosis, started once the thread has been quiet for ten minutes, and nothing it produces reaches Alex: it is
not delivered, and a question it asks is dropped rather than parked on him."""
import contextlib, io, json, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch

from agent_harness import make_agent, put
from agent_core import corrections, reply_envelope
from agent_core.reply_envelope import Reply
from test_follow_ups import telegram
import sqlite3
from owner_telegram import OWNER_CHAT_ID
CHAT = OWNER_CHAT_ID
T0 = 1_800_000_000.0


def agent_for(name, tmp):
    root = Path(tmp) / name; root.mkdir()
    with contextlib.redirect_stdout(io.StringIO()):
        return make_agent(name, root)


def feedback(agent, chat, messages, now=None):
    # Fixture supplies explicit ingress records; production never searches a transcript.
    user = max(n for n, m in enumerate(messages) if m['role'] == 'user')
    source = messages[user]['content']
    quoted = next((m['content'] for m in reversed(messages[:user]) if m['role'] == 'assistant'), '')
    ident = int((now or time.time()) * 1000)
    payload = telegram(ident, source)
    if quoted:
        payload['reply_to_message'] = telegram(ident - 1, quoted)
    db = sqlite3.connect(agent._inbox.database)
    try:
        db.execute('INSERT OR REPLACE INTO jobs(id,chat,message,handler,payload,status,created) VALUES (?,?,?,?,?,?,?)',
                   (ident, chat, ident, 'handle_message', json.dumps(payload), 'running', now or time.time()))
        db.commit()
    finally:
        db.close()
    agent._inbox.local.turn_messages = [(ident, source)]
    return corrections.start(agent, chat, [ident], now=now)


class Envelope(unittest.TestCase):
    def test_correction_is_a_required_declared_field(self):
        self.assertIn("correction", reply_envelope.SCHEMA["required"])
        r = reply_envelope.reply({"reply": "抱歉", "modality": "text", "work": "none", "correction": [12]})
        self.assertEqual(r.correction, (12,))
        self.assertEqual(reply_envelope.reply({"reply": "ok"}).correction, ())


class Diagnosis(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def conversation(self):
        return [{"role": "user", "content": "所以上次那个问题的结论是？"},
                {"role": "assistant", "content": "结论是……（状态更正：任务 241 在等您决定。）"},
                {"role": "user", "content": "[2026-10-20 22:08] 状态更正：任务 241 在等您决定。这都是啥？"},
                {"role": "assistant", "content": "那是内部任务编号，不该混进给你的回复。"}]

    def diagnoses(self, agent):
        return [t for t in agent._background.status(CHAT) if corrections.is_diagnosis(t["request"])]

    def test_a_thread_of_corrections_is_one_diagnosis_started_once_it_goes_quiet(self):
        agent = agent_for("cato", self.tmp.name)
        self.assertEqual(feedback(agent, CHAT, self.conversation(), now=T0), 1)
        more = self.conversation() + [{"role": "assistant", "content": "我把编号去掉了。"},
                                      {"role": "user", "content": "什么在等我决定"}]
        self.assertEqual(feedback(agent, CHAT, more, now=T0 + 60), 2)
        self.assertEqual(self.diagnoses(agent), [])                                    # nothing started yet
        self.assertEqual(corrections.flush(agent, now=T0 + 60 + corrections.QUIET_SECONDS - 1), [])
        started = corrections.flush(agent, now=T0 + 60 + corrections.QUIET_SECONDS)
        self.assertEqual(len(started), 1)
        task = agent._background.get(started[0])
        self.assertTrue(task["request"].startswith(corrections.PREFIX))
        self.assertIn("Recorded feedback", task["request"])
        self.assertIn("这都是啥", task["request"])
        self.assertIn("什么在等我决定", task["request"])
        self.assertIn("（状态更正：任务 241 在等您决定。）", task["request"])             # the reply he reacted to
        self.assertIn("我把编号去掉了。", task["request"])
        self.assertIn(str(agent.AGENT_DIR / "runtime_reports"), task["request"])
        self.assertTrue((agent.AGENT_DIR / "runtime_reports").is_dir())
        context = json.loads((agent._background.directory(started[0]) / "context.json").read_text(encoding="utf-8"))
        self.assertIs(context["notify_telegram"], False)                              # its record is never sent to him
        self.assertEqual(corrections.flush(agent, now=T0 + 10_000), [])                # the bundle was consumed
        with agent._inbox.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM usage_events WHERE kind='correction'").fetchone()[0], 2)

    def test_a_failure_the_runtime_recorded_joins_the_next_diagnosis(self):
        """GEPA's point (2026-10-24): a lesson that recurred or a declared check that failed is diagnosed from the
        actual run, in the same silent diagnosis as Alex's corrections, marked as the runtime's record, not his words."""
        agent = agent_for("cato", self.tmp.name)
        self.assertEqual(corrections.start_failure(agent, CHAT, "Lesson L7 recurred", "task 161: rebuilt twice", now=T0), 1)
        started = corrections.flush(agent, now=T0 + corrections.QUIET_SECONDS)
        self.assertEqual(len(started), 1)
        request = agent._background.get(started[0])["request"]
        self.assertIn("Recorded by the runtime, not Alex's words: Lesson L7 recurred", request)
        self.assertIn("Evidence: task 161: rebuilt twice", request)
        context = json.loads((agent._background.directory(started[0]) / "context.json").read_text(encoding="utf-8"))
        self.assertIs(context["notify_telegram"], False)
        tacitus = agent_for("tacitus", self.tmp.name)
        self.assertIsNone(corrections.start_failure(tacitus, CHAT, "Lesson L7 recurred", "x", now=T0))

    def test_a_later_thread_joins_a_diagnosis_still_running(self):
        agent = agent_for("cato", self.tmp.name)
        feedback(agent, CHAT, self.conversation(), now=T0)
        first = corrections.flush(agent, now=T0 + corrections.QUIET_SECONDS)[0]
        feedback(agent, CHAT, self.conversation() + [{"role": "user", "content": "还是不对"}], now=T0 + 700)
        self.assertEqual(corrections.flush(agent, now=T0 + 700 + corrections.QUIET_SECONDS), [first])
        self.assertEqual(len(self.diagnoses(agent)), 1)
        self.assertIn("还是不对", " ".join(c["text"] for c in agent._background.pending_controls(first)))

    def test_an_eval_or_trial_rerun_cannot_ask_owner_either(self):
        from agent_core import regression
        agent = agent_for("cato", self.tmp.name)
        store = agent._background
        ident = regression.start_rerun(store, CHAT, regression.load_cases(agent.AGENT_DIR)[0], 1)   # a benchmark case
        put(store, ident, "running")
        state = store.finish(ident, Reply("Which basket?", task_outcome="blocked", question="Which basket?"))
        self.assertEqual(state, "needs_attention")
        self.assertFalse(json.loads((store.directory(ident) / "completion.json").read_text())["passed"])
        self.assertEqual(store.decisions(CHAT, ident), [])

    def test_a_diagnosis_cannot_ask_owner_and_ends_finished(self):
        agent = agent_for("cato", self.tmp.name)
        store = agent._background
        feedback(agent, CHAT, self.conversation(), now=T0)
        ident = corrections.flush(agent, now=T0 + corrections.QUIET_SECONDS)[0]
        put(store, ident, "running")
        state = store.finish(ident, Reply("Lesson recorded. 是否批准这个修改？", task_outcome="blocked", question="是否批准这个修改？"))
        self.assertEqual(state, "finished")
        self.assertEqual(store.decisions(CHAT, ident), [])
        self.assertEqual(store.get(ident)["status"], "finished")

    def test_tacitus_and_workers_do_not_record_one(self):
        tacitus = agent_for("tacitus", self.tmp.name)
        self.assertIsNone(feedback(tacitus, CHAT, self.conversation()))
        cato = agent_for("cato", self.tmp.name)
        cato.BACKGROUND_TASK_ID = "5"
        self.assertIsNone(feedback(cato, CHAT, self.conversation()))
        self.assertFalse((cato.AGENT_DIR / "task_state" / "corrections_pending.json").exists())

    def test_the_turn_records_it_only_when_the_reply_says_corrected(self):
        agent = agent_for("cato", self.tmp.name)
        agent.BACKGROUND_TASK_ID = ""
        for flag, expected in (([], 0), ([12], 1)):
            messages = self.conversation()[:3]
            payload = telegram(12, messages[-1]['content'])
            db = sqlite3.connect(agent._inbox.database)
            try:
                db.execute('INSERT OR REPLACE INTO jobs(id,chat,message,handler,payload,status,created) VALUES (?,?,?,?,?,?,?)',
                           (12, CHAT, 12, 'handle_message', json.dumps(payload), 'running', T0))
                db.commit()
            finally:
                db.close()
            agent._inbox.local.turn_messages = [(12, messages[-1]['content'])]
            reply = Reply("那是内部任务编号，不该混进给你的回复。", correction=flag)
            with patch("agent_core.session_lifecycle.continue_turn", return_value=reply), \
                 patch("agent_core.task_decisions.verify_reply", side_effect=lambda a, m, c, r: r):
                out = agent.run_turn(messages, CHAT)
            self.assertEqual(str(out), "那是内部任务编号，不该混进给你的回复。")
            self.assertEqual(len(corrections._pending(agent).get(str(CHAT), [])), expected, flag)
            self.assertEqual(self.diagnoses(agent), [])                                # never a task in the turn itself


class LessonToolForEveryAgentThatLearns(unittest.TestCase):
    def test_cato_cal_and_vit_have_it_once_and_tacitus_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, has in (("cato", True), ("callimachus", True), ("vitruvius", True), ("tacitus", False)):
                names = [t["name"] for t in agent_for(name, tmp).get_tools("anthropic")]
                self.assertEqual(names.count("lesson"), 1 if has else 0, name)


if __name__ == "__main__":
    unittest.main()


class Burst(unittest.TestCase):
    """2026-10-23: after two corrections in a thread, the next reply restates, it does not fix."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.agent = agent_for("cato", self.tmp.name)
        self.agent.BACKGROUND_TASK_ID = ""

    def conversation(self, text):
        return [{"role": "assistant", "content": "这个阈值是 95 分位。"}, {"role": "user", "content": text}]

    def test_one_correction_is_not_a_burst_and_two_are(self):
        feedback(self.agent, CHAT, self.conversation("不对"), now=T0)
        self.assertEqual(corrections.burst_note(self.agent, CHAT, now=T0 + 30), "")
        feedback(self.agent, CHAT, self.conversation("你知道你在说什么吗"), now=T0 + 60)
        note = corrections.burst_note(self.agent, CHAT, now=T0 + 90)
        self.assertIn("corrected you 2 times", note)
        self.assertIn("say what you now understand he means", note)

    def test_the_note_fires_once_per_correction_not_once_per_turn(self):
        for t in (T0, T0 + 60):
            feedback(self.agent, CHAT, self.conversation("不对"), now=t)
        self.agent._inbox.local.job = 501                                      # the turn after the second correction
        self.assertIn("corrected you", corrections.burst_note(self.agent, CHAT, now=T0 + 70))
        self.assertIn("corrected you", corrections.burst_note(self.agent, CHAT, now=T0 + 75))   # same turn: stable
        self.agent._inbox.local.job = 502                                      # Alex said [quote removed]; next turn: no restating again
        self.assertEqual(corrections.burst_note(self.agent, CHAT, now=T0 + 100), "")
        feedback(self.agent, CHAT, self.conversation("还是不对"), now=T0 + 120)
        self.agent._inbox.local.job = 503
        self.assertIn("corrected you 3 times", corrections.burst_note(self.agent, CHAT, now=T0 + 130))
        corrections.flush(self.agent, now=T0 + 120 + corrections.QUIET_SECONDS)   # the thread ends; nothing lingers
        self.assertEqual(corrections._pending(self.agent).get("noted", {}).get(str(CHAT)), None)

    def test_the_note_lapses_once_the_thread_has_moved_on_and_never_reaches_a_worker(self):
        for t in (T0, T0 + 60):
            feedback(self.agent, CHAT, self.conversation("不对"), now=t)
        self.assertEqual(corrections.burst_note(self.agent, CHAT, now=T0 + 60 + corrections.BURST_RECENT_SECONDS + 1), "")
        self.agent.BACKGROUND_TASK_ID = "7"
        self.assertEqual(corrections.burst_note(self.agent, CHAT, now=T0 + 90), "")

    def test_the_note_rides_the_latest_message_on_the_wire_only(self):
        for t in (time.time() - 60, time.time() - 30):
            feedback(self.agent, CHAT, self.conversation("不对"), now=t)
        window = [{"role": "user", "content": "不对吧"}]
        wired = self.agent._attach_current_context(window, CHAT)
        text = wired[-1]["content"] if isinstance(wired[-1]["content"], str) else " ".join(b.get("text", "") for b in wired[-1]["content"])
        self.assertIn("corrected you 2 times", text)
        self.assertEqual(window, [{"role": "user", "content": "不对吧"}])       # the saved history is untouched
