"""The lesson backlog is triaged, not read whole: stale candidates expire, the digest groups repeats (2026-10-20)."""
import tempfile
import unittest
from pathlib import Path

from agent_core import learning


class Triage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.chat = 5

    def tearDown(self):
        self.tmp.cleanup()

    def test_expire_only_old_candidates(self):
        old = learning.propose(self.root, self.chat, "Always cite the filing date when quoting a 10-K figure", "task 1")
        new = learning.propose(self.root, self.chat, "Check the basket weights sum to one before sending", "task 2")
        gone = learning.expire(self.root, self.chat, days=30, now=learning.time.time() + 31 * 86400)
        self.assertEqual(sorted(gone), sorted([old, new]))                         # both are older than 30 days here
        self.assertEqual(learning.read(self.root, self.chat, old)["status"], "expired")
        again = learning.propose(self.root, self.chat, "Always cite the filing date when quoting a 10-K figure", "task 3")
        self.assertNotEqual(again, old)                                           # a repeat starts a fresh candidate
        self.assertEqual(learning.expire(self.root, self.chat, days=30), [])     # fresh ones stay

    def test_digest_groups_repeats_first(self):
        for i in range(3):
            learning.propose(self.root, self.chat, "Always cite the filing date when quoting a 10-K figure", f"task {i}")
        learning.propose(self.root, self.chat, "Check the basket weights sum to one before sending", "task 9")
        text = learning.digest(self.root, self.chat)
        self.assertIn("4 candidates in 2 themes", text)
        lines = text.splitlines()
        self.assertTrue(lines[1].startswith("- x3 "), lines)
        self.assertIn("filing date", lines[1])
        self.assertTrue(lines[2].startswith("- x1 "), lines)


if __name__ == "__main__":
    unittest.main()
