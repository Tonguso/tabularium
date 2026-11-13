"""The agent's own mailbox (tabularium.tools.agentmail, 2026-11-13): the key names the inbox, mail goes out as HTML
rendered from the agent's text, and a receipt is claimed before the one send."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tabularium.records.action_evidence import EmailReceipts
from tabularium.tools import agentmail
from tabularium.tools.agentmail import AgentMail, html, addresses


def response(status, body):
    r = Mock(status_code=status, content=b"x", text=str(body))
    r.json.return_value = body
    return r


class Client(unittest.TestCase):
    def test_the_key_names_the_inbox_and_the_send_carries_text_and_html(self):
        calls = []
        def request(method, url, **kw):
            calls.append((method, url, kw.get("json")))
            if url.endswith("/auth/me"):
                return response(200, {"scope_type": "inbox", "inbox_id": "agent@agentmail.to"})
            return response(200, {"message_id": "m1", "thread_id": "t1"})
        with patch.object(agentmail.requests, "request", request):
            mail = AgentMail("am_key")
            self.assertEqual(mail.send(["a@example.test"], "Hi", "One\n\nTwo **bold**"), {"message_id": "m1", "thread_id": "t1"})
        method, url, body = calls[-1]
        self.assertEqual((method, url), ("POST", agentmail.BASE + "/inboxes/agent@agentmail.to/messages/send"))
        self.assertEqual(body["text"], "One\n\nTwo **bold**")
        self.assertIn("<p>One</p><p>Two <strong>bold</strong></p>", body["html"])
        self.assertEqual(len([c for c in calls if c[1].endswith("/auth/me")]), 1)     # learnt once

    def test_a_reply_threads_under_the_message_and_keeps_its_subject(self):
        mail = AgentMail("am_key"); mail.inbox = "agent@agentmail.to"
        with patch.object(agentmail.requests, "request", return_value=response(200, {"message_id": "m2", "thread_id": "t1"})) as req:
            mail.send(["a@example.test"], "ignored", "Thanks", reply_to_id="m1")
        self.assertTrue(req.call_args.args[1].endswith("/messages/m1/reply"))
        self.assertNotIn("subject", req.call_args.kwargs["json"])

    def test_the_services_no_is_an_error_with_its_fix(self):
        mail = AgentMail("am_key"); mail.inbox = "agent@agentmail.to"
        with patch.object(agentmail.requests, "request", return_value=response(403, {"code": "missing_permission", "fix": "Use a key with message_send."})):
            with self.assertRaisesRegex(RuntimeError, "message_send"):
                mail.send(["a@example.test"], "s", "b")

    def test_html_renders_the_agents_markdown_shaped_text(self):
        out = html("# Title\n\nFirst line\nsecond line\n\n- one\n- two <b>\n\n1. a\n2. b\n\nSee https://x.test/p `code`")
        self.assertIn("<h1>Title</h1>", out)
        self.assertIn("<p>First line<br>second line</p>", out)
        self.assertIn("<ul><li>one</li><li>two &lt;b&gt;</li></ul>", out)
        self.assertIn("<ol><li>a</li><li>b</li></ol>", out)
        self.assertIn('<a href="https://x.test/p">https://x.test/p</a> <code>code</code>', out)
        self.assertEqual(addresses("a@x; b@y, c@z"), ["a@x", "b@y", "c@z"])


class Receipts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = EmailReceipts(Path(self.tmp.name) / "receipts")
        self.mail = Mock()
        self.mail.send.return_value = {"message_id": "m1", "thread_id": "t1"}
        self.payload = dict(to="a@example.test; b@example.test", subject="S", body="B", cc="", reply_to_id="",
                            from_address="agent@agentmail.to", attachments=[])

    def tearDown(self):
        self.tmp.cleanup()

    def test_one_send_per_payload_and_the_receipt_says_so(self):
        receipt = self.store.send(self.mail, self.payload)
        self.assertEqual((receipt["status"], receipt["message_id"]), ("confirmed_sent", "m1"))
        self.assertIn("delivery unverified", receipt["evidence"])
        self.mail.send.assert_called_once_with(["a@example.test", "b@example.test"], "S", "B", [], [], "")
        again = self.store.send(self.mail, self.payload)
        self.assertEqual((again["id"], again["status"]), (receipt["id"], "confirmed_sent"))
        self.assertIn("Not sent again", again["replay"])
        self.assertEqual(self.mail.send.call_count, 1)
        self.assertEqual(self.store.get(receipt["id"])["message_id"], "m1")

    def test_no_answer_keeps_the_outcome_unknown_and_never_resends(self):
        self.mail.send.side_effect = TimeoutError()
        receipt = self.store.send(self.mail, self.payload)
        self.assertEqual(receipt["status"], "send_outcome_unknown")
        self.store.send(self.mail, self.payload)
        self.assertEqual(self.mail.send.call_count, 1)

    def test_a_rejection_is_recorded_and_a_corrected_attempt_may_follow(self):
        self.mail.send.side_effect = [RuntimeError("AgentMail 400: bad address"), {"message_id": "m2", "thread_id": "t2"}]
        receipt = self.store.send(self.mail, self.payload)
        self.assertEqual((receipt["status"], receipt["error"]), ("rejected", "AgentMail 400: bad address"))
        second = self.store.send(self.mail, self.payload)
        self.assertEqual((second["status"], second["message_id"]), ("confirmed_sent", "m2"))
        self.assertNotEqual(second["id"], receipt["id"])

    def test_a_missing_attachment_sends_nothing(self):
        receipt = self.store.send(self.mail, dict(self.payload, attachments=[str(Path(self.tmp.name) / "gone.txt")]))
        self.assertEqual(receipt["status"], "not_sent")
        self.mail.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
