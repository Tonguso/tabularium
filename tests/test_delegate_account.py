"""Item 2 (2026-10-24): the delegate's own DONE / OPEN / EVIDENCE / COST lines travel unedited under Cato's verdict.

Tiro was called ready three times on Cato's reading of Vit's reports. Now the result is kept as fetched and its head
lines are appended to the owner task's message, so Alex reads what Vit claimed and what he left open."""
import json, tempfile, unittest
from pathlib import Path

from agent_harness import make_agent


class DelegateAccountReachesAlex(unittest.TestCase):
    """Item 2 (2026-10-24): the delegate's own DONE / OPEN / EVIDENCE / COST lines travel unedited under Cato's verdict."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / "cato"; root.mkdir()
        self.agent = make_agent("cato", root)
        self.store = self.agent._background
        self.task = self.store.start(1, "Receive Vit's work: Tiro")["id"]
        self.store.add_delegation(self.task, "vit", "req-tiro", "Tiro build", "owner_decision")
        items = [dict(item="Tiro launches", check="launch it", critical=True, early=True)]
        from tests.test_delegation_agreement import bind_delegate
        self.remote, self.child = bind_delegate(self.store, self.task, "req-tiro", items)
        self.store.record_submission("req-tiro", self.task, "vit", "Tiro build", 250000, items)

    def test_the_result_is_kept_as_fetched_and_its_head_lines_are_quoted(self):
        text = ("DONE: items 1 and 3. Item 2 not tested.\nOPEN: nobody has tried drag-and-drop on another PC.\n"
                "EVIDENCE: D:/Agents/vitruvius/reports/tiro/report.md\nCOST: about 9.4M effective tokens.\n\nD:/Tiro is built.")
        (self.remote.directory(self.child)/"result.txt").write_text(text, encoding="utf-8")
        runner = lambda argv: type("R", (), {"returncode": 0, "stdout": json.dumps({"status": "completed", "text": text}), "stderr": ""})()
        self.agent.tool_delegate(action="result", chat_id=1, agent="vit", request_id="req-tiro", task_id=self.task, _runner=runner)
        kept = self.store.directory(self.task) / "delegate_results" / "req-tiro.txt"
        self.assertEqual(kept.read_text(encoding="utf-8"), text)
        account = self.store.delegate_account(self.task)
        self.assertTrue(account.startswith("Vit's own account (Tiro build), unedited:\n"))
        self.assertIn("OPEN: nobody has tried drag-and-drop on another PC.", account)
        self.assertIn("COST: about 9.4M effective tokens.", account)
        self.assertNotIn("D:/Tiro is built.", account)                              # body stays Cato's to read

    def test_a_result_without_head_lines_is_said_to_be_cato_s_reading_only(self):
        self.store.mark_delegation(self.task, "req-tiro", "returned")
        (self.remote.directory(self.child)/"result.txt").write_text("Built it. Works on my machine.", encoding="utf-8")
        self.store.save_delegate_result(self.task, "req-tiro", "Built it. Works on my machine.")
        self.assertIn("carried no DONE / OPEN / EVIDENCE / COST lines; only Cato's reading above", self.store.delegate_account(self.task))


if __name__ == "__main__":
    unittest.main()
