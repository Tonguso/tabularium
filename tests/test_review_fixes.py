"""Codex's framework review (2026-10-20), items 5, 6 and 8: each test reproduces what he found.

5. A memory tidy-up built before a correction landed wiped that correction when applied.
6. The eval passed an answer when the judge checked zero items.
8. Nothing recorded which code a task ran; a restart could switch the agents to uncommitted edits.
(Item 7, token double counting, is in test_trace; item 3, the second-model status audit, in test_task_decisions.)"""
import json, subprocess, tempfile, unittest
from pathlib import Path

from tabularium.memory import memory_consolidation as mc

from tabularium.ops import release
from tabularium.ops import agent_control
class MemoryLostUpdate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "task_state").mkdir()
        (self.root / "memory.md").write_text("# Core\n- tea\n- tea, a lot\n", encoding="utf-8")

    def trialled(self):
        meta = self.root / "task_state" / "memory_candidate.json"
        record = json.loads(meta.read_text(encoding="utf-8"))
        record["trial"] = {"before": {"correct": 1, "partial": 0, "wrong": 0, "error": ""},
                           "after": {"correct": 1, "partial": 0, "wrong": 0, "error": ""}, "items": []}
        meta.write_text(json.dumps(record), encoding="utf-8")

    def test_a_correction_after_the_candidate_is_not_lost(self):
        mc.propose(self.root, "- tea, a lot\n", "")                     # 1. the tidy-up candidate
        live = self.root / "memory.md"
        live.write_text(live.read_text(encoding="utf-8") + "- Rent is due on the 3rd, not the 1st\n", encoding="utf-8")   # 2.
        self.trialled()
        with self.assertRaisesRegex(ValueError, "changed after this candidate"):
            mc.apply(self.root)                                         # 3. refused, not applied over it
        self.assertIn("rebase", mc.summary(self.root))
        self.assertEqual(mc.rebase(self.root), 1)                       # the edit replayed on the current file
        with self.assertRaisesRegex(ValueError, "trial"):
            mc.apply(self.root)                                         # the old trial no longer applies
        self.trialled()
        mc.apply(self.root)
        text = live.read_text(encoding="utf-8")
        self.assertIn("Rent is due on the 3rd", text)                   # 4. the correction survives
        self.assertNotIn("tea, a lot", text)

    def test_rebase_stops_when_an_edit_no_longer_fits(self):
        mc.propose(self.root, "- tea, a lot\n", "")
        (self.root / "memory.md").write_text("# Core\n- tea\n", encoding="utf-8")   # someone removed it already
        with self.assertRaisesRegex(ValueError, "no longer applies"):
            mc.rebase(self.root)


class Release(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        git = lambda *a: subprocess.run(["git", "-C", str(self.root), *a], capture_output=True, check=True)
        git("init", "-q"); git("config", "user.email", "t@t"); git("config", "user.name", "t")
        (self.root / "engine.py").write_text("x = 1\n", encoding="utf-8")
        git("add", "."); git("commit", "-q", "-m", "one")
        self.head = git("rev-parse", "HEAD").stdout.decode().strip()

    def test_the_commit_is_read_without_running_git(self):
        self.assertEqual(release.commit(self.root), self.head)
        self.assertEqual(release.commit(self.root / "missing"), "")

    def test_release_refuses_uncommitted_changes(self):
        (self.root/'engine.py').write_text('x = 2\n',encoding='utf-8')
        self.assertEqual(release.uncommitted(self.root),['engine.py'])
        self.assertRaisesRegex(RuntimeError,'Commit tracked changes',agent_control.request_restart,['cato'],root=self.root)
        self.assertFalse((self.root/'cato/task_state/drain').exists())
        self.assertFalse((self.root/'dashboard/restart_status.json').exists())

    def test_a_worker_attempt_records_its_release(self):
        from tabularium.records.background_runtime import TaskStore
        from tabularium.records import lifecycle
        store = TaskStore(self.root / "cato")
        ident = store.start(1, "work")["id"]
        store.claim_next()
        with store.db() as db:
            lifecycle.move(db, ident, "run", "running", "Worker started", release=release.commit(self.root))
        self.assertEqual(store.get(ident)["release"], self.head)


if __name__ == "__main__":
    unittest.main()
