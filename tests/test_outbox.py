"""Messages to Alex survive a Telegram outage; credentials never reach a log (2026-10-21).

22:11-22:14 the PC lost its route to api.telegram.org. Cato's reply to Alex's 22:11 message failed once and was
dropped, and the error line printed the request URL with the bot token into cato.log."""
import contextlib, io, sqlite3, tempfile, time, unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
import unittest.mock

import requests
from telebot.apihelper import ApiTelegramException

from agent_harness import make_agent
from tabularium.records import outbox as outbox_mod
from tabularium.records.outbox import Outbox
from tabularium.core import agent_logging
from tabularium.channels.owner_telegram import OWNER_CHAT_ID
CHAT = OWNER_CHAT_ID


def refusal(code, text):
    return ApiTelegramException("sendMessage", SimpleNamespace(status_code=code, text=text),
                                {"error_code": code, "description": text})


class FakeBot:
    def __init__(self):
        self.down, self.sent, self.refuse = False, [], None
    def send_message(self, chat, text, **kw):
        if self.down:
            raise requests.exceptions.ConnectionError("Max retries exceeded with url: /bot<token>/sendMessage")
        if self.refuse:
            exc = self.refuse(kw)
            if exc:
                raise exc
        self.sent.append((text, kw.get("reply_to_message_id"), kw.get("parse_mode")))
        return SimpleNamespace(message_id=1000 + len(self.sent))


class Delivery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bot = FakeBot()
        self.asked = []
        self.box = Outbox(self.tmp.name, lambda: self.bot, lambda d, m: self.asked.append((d, m)))

    def test_an_outage_queues_in_order_and_the_retry_delivers_everything(self):
        self.bot.down = True
        self.assertEqual(self.box.put(CHAT, ["reply to 22:11"], reply_to=11), [])
        self.assertEqual(self.box.put(CHAT, ["part 1", "part 2"]), [])
        self.assertEqual(len(self.box.pending(CHAT)), 3)
        self.bot.down = False
        self.assertEqual(len(self.box.flush()), 3)
        self.assertEqual([t for t, _, _ in self.bot.sent], ["reply to 22:11", "part 1", "part 2"])
        self.assertEqual(self.bot.sent[0][1], 11)                    # the quote survives the wait
        self.assertEqual(self.box.pending(), [])

    def test_a_retry_after_a_transient_failure_drops_the_connection_pool_first(self):
        """21:47 2026-10-22: a 502 then read timeouts on every retry for minutes, while curl reached Telegram at
        once: the bot's pooled connection was dead. The retry loop resets the pool before retrying."""
        resets = []
        box = Outbox(self.tmp.name, lambda: self.bot, None, reset=lambda: resets.append(True))
        self.bot.down = True
        box.put(CHAT, ["stuck reply"])
        # Drive the real retry loop synchronously: failed retry, recovered retry, then stop.
        # No daemon can outlive this test and keep its temporary SQLite database open.
        class EndLoop(BaseException):
            pass
        waits = []
        def wait(delay):
            waits.append(delay)
            if len(waits) == 3:
                raise EndLoop()
            self.bot.down = len(waits) == 1
        box.wake = unittest.mock.Mock()
        box.wake.wait.side_effect = wait
        patch = unittest.mock.patch.object(outbox_mod.threading, "Thread")
        thread = patch.start()
        self.addCleanup(patch.stop)
        box.start()
        thread.return_value.start.assert_called_once()
        self.assertRaises(EndLoop, thread.call_args.kwargs['target'])
        self.assertEqual([t for t, _, _ in self.bot.sent], ["stuck reply"])
        self.assertEqual(resets, [True])
        self.assertEqual(box.pending(), [])

    def test_a_stall_is_logged_once_per_message_as_it_starts(self):
        self.bot.down = True
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.box.put(CHAT, ["reply"])
            self.box.flush(); self.box.flush()
        self.assertEqual(out.getvalue().count("[outbox] message"), 1)
        self.assertIn("will retry: ConnectionError", out.getvalue())

    def test_a_long_delay_says_when_the_message_was_written(self):
        self.bot.down = True
        self.box.put(CHAT, ["late answer"])
        with closing(sqlite3.connect(self.box.path)) as db, db:
            db.execute("UPDATE outbox SET created=?", (time.time() - 600,))
        self.bot.down = False
        self.box.flush()
        self.assertTrue(self.bot.sent[0][0].startswith("[Written "))
        self.assertIn("delayed by a connection outage]\nlate answer", self.bot.sent[0][0])

    def test_telegram_refusals_fall_back_and_never_loop(self):
        # the quoted message is gone: sent without the quote
        self.bot.refuse = lambda kw: refusal(400, "Bad Request: message to be replied not found") if kw.get("reply_to_message_id") else None
        self.box.put(CHAT, ["answer"], reply_to=5)
        self.assertEqual(self.bot.sent[-1][:2], ("answer", None))
        # malformed HTML: sent as plain text
        self.bot.refuse = lambda kw: refusal(400, "Bad Request: can't parse entities") if kw.get("parse_mode") else None
        self.box.put(CHAT, ["<b>bold</b> &amp; more"])
        self.assertEqual(self.bot.sent[-1][0], "bold & more")
        # refused outright: marked failed, not retried forever
        self.bot.refuse = lambda kw: refusal(403, "Forbidden: bot was blocked by the user")
        with contextlib.redirect_stdout(io.StringIO()):
            self.box.put(CHAT, ["x"])
        self.assertEqual(self.box.pending(), [])

    def test_rate_limit_and_server_errors_are_retried(self):
        for code in (429, 502):
            self.bot.refuse = lambda kw, code=code: refusal(code, "try later")
            self.box.put(CHAT, [f"m{code}"])
            self.assertEqual(len(self.box.pending()), 1)
            self.bot.refuse = None
            self.box.flush()
            self.assertEqual(self.box.pending(), [])

    def test_a_question_delivered_after_the_outage_is_still_bound_to_its_decision(self):
        self.bot.down = True
        self.box.put(CHAT, ["30 names or 50?"], decision=7)
        self.bot.down = False
        self.box.flush()
        self.assertEqual(self.asked, [(7, 1001)])


class AgentPath(unittest.TestCase):
    def test_send_message_accepts_during_an_outage_and_delivers_after(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "cato"; root.mkdir()
            with contextlib.redirect_stdout(io.StringIO()):
                agent = make_agent("cato", root)
            agent.bot = FakeBot(); agent.bot.down = True
            self.assertTrue(agent.send_message(CHAT, "The default subtitle should match your selection."))
            self.assertEqual(len(agent.outbox().pending()), 1)
            agent.bot.down = False
            agent.outbox().flush()
            self.assertEqual(agent.bot.sent[0][0], "The default subtitle should match your selection.")


class NoCredentialsInLogs(unittest.TestCase):
    TOKEN = "1234567890:AAFmbVwBh6EiWkdlJMqRDoKjGEeaZSl5FpX"

    def test_the_log_writer_redacts_tokens_and_keys(self):
        line = f"send_message error: Max retries exceeded with url: /bot{self.TOKEN}/sendMessage?chat_id=123456789"
        out = agent_logging.redact(line + " sk-ant-api03-abcdefghijklmnopqrstuvwxyz")
        self.assertNotIn(self.TOKEN, out)
        self.assertNotIn("sk-ant-api03", out)
        self.assertIn("chat_id=123456789", out)                     # ordinary numbers stay

    def test_existing_files_are_masked_in_place_at_the_same_length(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "cato-launch.log"
            text = f"before /bot{self.TOKEN}/sendMessage after\n"
            log.write_text(text, encoding="utf-8")
            self.assertEqual(agent_logging.mask_file(log), 1)
            masked = log.read_text(encoding="utf-8")
            self.assertNotIn(self.TOKEN, masked)
            self.assertEqual(len(masked), len(text))
            self.assertEqual(agent_logging.mask_file(log), 0)


if __name__ == "__main__":
    unittest.main()
