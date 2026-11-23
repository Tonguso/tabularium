"""The owner's corrections become the evaluation (2026-11-23).

A diagnosis found about one in five of his messages was a correction while the synthetic benchmark passed almost
every case: the evaluation could not see what he corrected. A case is rebuilt from the inbox and the outbox, the
agent answers its prompt in a rehearsal that records nothing, and a judge says whether the fault is repeated."""
import contextlib, io, json, sqlite3, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch

from agent_harness import make_agent
from tabularium.channels.owner_telegram import OWNER_CHAT_ID
from tabularium.core.agent_runtime import DurableInbox
from tabularium.core.reply_envelope import Reply
from tabularium.learning import correction_eval as ce
from tabularium.records.outbox import Outbox
CHAT = OWNER_CHAT_ID
DAY = 86400


class Records:
    """An agent folder with a real inbox and outbox, filled by hand."""

    def __init__(self, root):
        self.root = Path(root)
        self.inbox = DurableInbox(self.root, lambda *a: None)
        self.outbox = Outbox(self.root, lambda: None)
        self.ids = 100

    def said(self, text, t, reply_to=None, channel=None):
        self.ids += 1
        payload = {"message_id": self.ids, "text": text, "date": int(t)}
        if reply_to:
            payload["reply_to_message"] = {"message_id": reply_to}
        if channel:
            payload["channel"] = channel
        with self.inbox.db() as db:
            db.execute("INSERT INTO jobs(chat,message,handler,payload,status,created) VALUES (?,?,?,?,?,?)",
                       (CHAT, self.ids, "handle_message", json.dumps(payload), "done", t))
        return self.ids

    def replied(self, text, t):
        self.ids += 1
        with contextlib.closing(sqlite3.connect(self.outbox.path)) as db, db:
            db.execute("INSERT INTO outbox(chat,text,first,status,created,message_id,original,channel) VALUES (?,?,1,'sent',?,?,?,'telegram')",
                       (CHAT, text, t, self.ids, text))
        return self.ids

    def corrected(self, message_id):
        with self.inbox.db() as db:
            db.execute("INSERT INTO usage_events(job,created,kind,provider,model,billing,role,metrics) VALUES (1,?,?,?,?,?,?,?)",
                       (time.time(), "correction", "", "owner", "", "foreground", json.dumps({"details": {"message_id": message_id}})))


class Cases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.r = Records(self.tmp.name)
        self.addCleanup(self.r.inbox.close)
        self.now = 1_800_000_000.0
        self.old = self.now - 10 * DAY

    def build(self, **kw):
        return ce.build_cases(self.tmp.name, CHAT, now=self.now, **kw)

    def test_a_case_is_the_conversation_up_to_the_corrected_reply_and_his_words_about_it(self):
        t = self.old
        self.r.said("What is the plan for the deck?", t)
        self.r.replied("Twelve slides, due Friday.", t + 10)
        self.r.said("Send the draft to the desk", t + 100)
        self.r.said("today please", t + 102)
        self.r.replied("Shall I send it now, or would you like to review it first?", t + 130)
        wrong = self.r.said("I just told you to send it. Stop asking.", t + 160)
        self.r.corrected(wrong)
        case, = self.build()
        self.assertEqual(case["id"], wrong)
        self.assertEqual(case["corrections"], ["I just told you to send it. Stop asking."])
        self.assertEqual(case["reply"], "Shall I send it now, or would you like to review it first?")
        self.assertEqual([p["text"] for p in case["prompt"]], ["Send the draft to the desk", "today please"])
        self.assertEqual([(c["role"], c["text"]) for c in case["context"]],
                         [("user", "What is the plan for the deck?"), ("assistant", "Twelve slides, due Friday.")])

    def test_a_correction_that_quotes_a_reply_is_about_that_reply_however_long_ago(self):
        t = self.old
        self.r.said("Name the fund", t)
        quoted = self.r.replied("The fund is Alpha.", t + 10)
        self.r.said("And the fee?", t + 20)
        self.r.replied("Two percent.", t + 30)
        wrong = self.r.said("It is Beta, not Alpha", t + 5 * 3600, reply_to=quoted)
        self.r.corrected(wrong)
        case, = self.build()
        self.assertEqual(case["reply"], "The fund is Alpha.")
        self.assertEqual([p["text"] for p in case["prompt"]], ["Name the fund"])

    def test_what_the_records_cannot_tie_to_a_reply_is_left_out(self):
        t = self.old
        self.r.said("Start the report", t)
        self.r.replied("Started.", t + 10)
        late = self.r.said("That was wrong", t + 3 * 3600)                    # hours later, quoting nothing
        self.r.corrected(late)
        self.r.replied("The report is ready.", t + 5 * 3600)                 # a result sent unprompted
        unprompted = self.r.said("Too long", t + 5 * 3600 + 30)
        self.r.corrected(unprompted)
        fresh_at = self.now - DAY                                            # still inside the window of rulings
        self.r.said("Draft the note", fresh_at)
        self.r.replied("Here is the note.", fresh_at + 10)
        fresh = self.r.said("Wrong tone", fresh_at + 40)
        self.r.corrected(fresh)
        self.r.corrected(999999)                                             # a message the inbox no longer holds
        self.assertEqual(self.build(), [])

    def test_two_corrections_of_one_reply_are_one_case_and_a_tap_is_not_something_said(self):
        t = self.old
        self.r.said("Make the logo", t)
        self.r.replied("Here it is.", t + 10)
        first = self.r.said("The strip is misaligned", t + 40)
        self.r.said("yes", t + 45, channel="button")
        second = self.r.said("And the red is wrong", t + 60)
        self.r.corrected(first); self.r.corrected(second)
        case, = self.build()
        self.assertEqual(case["id"], second)                                 # newest first
        self.assertEqual(sorted(case["corrections"]), ["And the red is wrong", "The strip is misaligned"])
        self.assertEqual([p["text"] for p in case["prompt"]], ["Make the logo"])

    def test_the_newest_cases_are_the_set(self):
        for n in range(4):
            t = self.old + n * 1000
            self.r.said(f"Request {n}", t)
            self.r.replied(f"Reply {n}", t + 10)
            self.r.corrected(self.r.said(f"Wrong {n}", t + 40))
        self.assertEqual([c["corrections"][0] for c in self.build(limit=2)], ["Wrong 3", "Wrong 2"])


class LiveRate(unittest.TestCase):
    def test_the_live_rate_counts_what_he_corrected_among_what_he_sent_this_week(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = Records(tmp)
            self.addCleanup(r.inbox.close)
            now = time.time()
            r.said("Old message", now - 30 * DAY)
            r.said("Make the logo", now - DAY)
            wrong = r.said("The strip is misaligned", now - DAY + 60)
            r.said("yes", now - DAY + 90, channel="button")                     # a tap is not a message
            r.corrected(wrong)
            self.assertEqual(ce.live_rate(tmp, CHAT, now=now), {"corrections": 1, "messages": 2})


CASE = {"id": 7, "said_at": "2026-11-10 10:00", "corrections": ["Stop asking, just send it."],
        "reply": "Shall I send it now?", "prompt": [{"t": 1_799_000_000.0, "text": "Send the draft to the desk"}],
        "context": [{"role": "user", "t": 1_798_999_000.0, "text": "What is the plan?"},
                    {"role": "assistant", "t": 1_798_999_010.0, "text": "Twelve slides."}]}


class Run(unittest.TestCase):
    def test_verdicts_are_counted_and_the_rate_leaves_out_what_cannot_be_told(self):
        answers = iter(["Sent.", "Shall I send it?", "I would use send_email here.", "Sent."])
        verdicts = iter(['{"fault":"asked instead of acting","verdict":"avoided","note":"it sent"}',
                         '{"fault":"asked instead of acting","verdict":"repeated","note":"asks again"}',
                         '{"fault":"asked","verdict":"unclear","note":"cannot tell"}', "not json"])
        seen = []
        record = ce.run(None, CHAT, cases=[dict(CASE, id=i) for i in range(4)], ask=lambda case: next(answers),
                        judge=lambda prompt: seen.append(prompt) or next(verdicts))
        self.assertEqual((record["repeated"], record["avoided"], record["unclear"]), (1, 1, 2))
        self.assertEqual(record["rate"], 0.5)
        self.assertEqual([i["verdict"] for i in record["items"]], ["avoided", "repeated", "unclear", "unclear"])
        for part in ("Send the draft to the desk", "Shall I send it now?", "Stop asking, just send it.", "Sent."):
            self.assertIn(part, seen[0])
        self.assertEqual(ce.summary(record), "1 of 2 corrections repeated, 2 unclear")

    def test_the_run_stops_when_the_owner_writes(self):
        quiet = iter([True, False])
        record = ce.run(None, CHAT, cases=[dict(CASE, id=i) for i in range(3)], ask=lambda case: "Sent.",
                        judge=lambda prompt: '{"fault":"f","verdict":"avoided","note":"n"}', quiet=lambda: next(quiet))
        self.assertEqual(len(record["items"]), 1)
        self.assertIn("stopped", record)

    def test_two_runs_are_compared_over_the_cases_they_share(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = lambda ident, verdict: {"id": ident, "verdict": verdict}
            ce.save(tmp, {"time": 1000.0, "items": [item(1, "repeated"), item(2, "repeated"), item(3, "unclear"), item(9, "repeated")],
                          "repeated": 3, "avoided": 0, "unclear": 1, "rate": 1.0, "error": "", "release": "a"})
            ce.save(tmp, {"time": 90000.0, "items": [item(1, "avoided"), item(2, "repeated"), item(3, "avoided"), item(4, "repeated")],
                          "repeated": 2, "avoided": 2, "unclear": 0, "rate": 0.5, "error": "", "release": "b"})
            latest = ce.latest(tmp)
        self.assertEqual((latest["rate"], latest["release"]), (0.5, "b"))
        self.assertEqual(latest["previous"], {"shared": 2, "repeated_then": 2, "repeated_now": 1})


class Rehearsal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / "coordinator"; root.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            self.agent = make_agent("coordinator", root)
        self.agent.BACKGROUND_TASK_ID = ""

    def reply(self, *a, **k):
        return Reply("Which fund do you mean?", question="Which fund do you mean?", correction=(5,))

    def test_a_rehearsed_reply_asks_nothing_and_records_nothing_and_only_its_tools_exist(self):
        with patch.object(self.agent._routing, "run", side_effect=self.reply):
            new = ce.answer(self.agent, CHAT, CASE)
        self.assertEqual(new, "Which fund do you mean?")
        self.assertEqual(self.agent._background.decisions(CHAT), [])
        self.assertIsNone(self.agent._tool_scope)                             # the scope ends with the rehearsal
        self.assertFalse(self.agent._rehearsing)
        # The same reply in a real turn does declare its question: the rehearsal is what kept it out.
        with patch.object(self.agent._routing, "run", side_effect=self.reply), contextlib.redirect_stdout(io.StringIO()):
            self.agent.run_turn([{"role": "user", "content": "Name the fund"}], CHAT)
        self.assertEqual([d["question"] for d in self.agent._background.decisions(CHAT)], ["Which fund do you mean?"])

    def test_the_replay_is_the_context_then_the_prompt_marked_as_a_rehearsal(self):
        seen = {}
        def run(agent, messages, chat):
            seen["messages"], seen["scope"] = [dict(m) for m in messages], set(agent._tool_scope)
            return Reply("Sent.")
        with patch.object(self.agent._routing, "run", side_effect=run):
            ce.answer(self.agent, CHAT, CASE)
        said = seen["messages"][2:]                                           # after the fixed opening
        self.assertEqual([m["role"] for m in said], ["user", "assistant", "user"])
        self.assertIn("What is the plan?", said[0]["content"])
        self.assertTrue(said[2]["content"].startswith(ce.REPLAY_PREFIX))
        self.assertIn("Send the draft to the desk", said[2]["content"])
        self.assertEqual(seen["scope"], set(ce.REPLAY_TOOLS))

    def test_a_run_is_due_once_a_day_and_only_when_the_owner_has_been_quiet(self):
        ran = []
        record = {"time": time.time(), "items": [], "repeated": 0, "avoided": 0, "unclear": 0, "rate": None, "error": "", "seconds": 0}
        with patch.object(ce, "run", side_effect=lambda *a, **k: ran.append(1) or dict(record)), contextlib.redirect_stdout(io.StringIO()):
            with patch.object(self.agent._inbox, "idle_seconds", return_value=60):
                self.assertIsNone(self.agent.run_correction_eval())            # he wrote a minute ago
            with patch.object(self.agent._inbox, "idle_seconds", return_value=3600):
                self.assertIsNotNone(self.agent.run_correction_eval())
                self.assertIsNone(self.agent.run_correction_eval())            # today's run exists
                (self.agent.AGENT_DIR / "task_state" / "correction_eval_requested").touch()
                time.sleep(1.1)                                                # a second run file needs a later second
                self.assertIsNotNone(self.agent.run_correction_eval())         # asked for by hand
        self.assertEqual(len(ran), 2)


if __name__ == "__main__":
    unittest.main()
