"""Retention (2026-10-11): trials are deleted, finished task folders and old context files are zipped; live tasks and
recent files are untouched."""
import os, sqlite3, sys, tempfile, time, unittest, zipfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import retention


def touch(path, age_days, now, content=b"x" * 100):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    t = now - age_days * 86400
    os.utime(path, (t, t))


class Retention(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name); self.now = time.time()
        retention.LOG = self.root / "retention.log"

    def tearDown(self):
        self.tmp.cleanup()

    def test_trials_old_deleted_recent_kept(self):
        touch(self.root / "cato" / "scratch" / "tools_trial" / "docling" / "model.bin", 10, self.now)
        touch(self.root / "cato" / "scratch" / "tools_trial" / "fresh" / "a.py", 1, self.now)   # newest file decides
        touch(self.root / "cato" / "scratch" / "notes.txt", 100, self.now)                      # not a trial dir
        actions = retention.sweep_trials(self.root, self.now)
        self.assertEqual(actions, [])                                                            # whole tools_trial is 1 day old
        os.utime(self.root / "cato" / "scratch" / "tools_trial" / "fresh" / "a.py", (self.now - 9 * 86400,) * 2)
        actions = retention.sweep_trials(self.root, self.now)
        self.assertEqual(len(actions), 1)
        self.assertFalse((self.root / "cato" / "scratch" / "tools_trial").exists())
        self.assertTrue((self.root / "cato" / "scratch" / "notes.txt").exists())

    def test_smoke_folders_anywhere_under_scratch_are_trials(self):
        deep = self.root / "cato" / "scratch" / "agent_refactor" / "tmp"
        touch(deep / "smoke_cato_ab12" / "SKILL.md", 9, self.now)                                 # old: deleted
        touch(deep / "smoke_tacitus_cd34" / "SKILL.md", 2, self.now)                               # recent: kept
        touch(deep / "report.md", 30, self.now)                                                  # not a smoke dir
        actions = retention.sweep_trials(self.root, self.now)
        self.assertEqual(len(actions), 1, actions)
        self.assertFalse((deep / "smoke_cato_ab12").exists())
        self.assertTrue((deep / "smoke_tacitus_cd34").exists())
        self.assertTrue((deep / "report.md").exists())

    def test_finished_tasks_zipped_live_tasks_kept(self):
        base = self.root / "cato" / "task_state" / "background"
        base.mkdir(parents=True)
        con = sqlite3.connect(base / "jobs.sqlite3")
        con.execute("CREATE TABLE tasks(id INTEGER PRIMARY KEY, status TEXT)")
        con.executemany("INSERT INTO tasks VALUES (?,?)", [(1, "finished"), (2, "running"), (3, "finished")]); con.commit(); con.close()
        touch(base / "1" / "result.txt", 20, self.now); touch(base / "1" / "task_state" / "inbox.sqlite3", 20, self.now)
        touch(base / "2" / "result.txt", 20, self.now)
        touch(base / "3" / "result.txt", 2, self.now)
        actions = retention.sweep_tasks(self.root, self.now)
        self.assertEqual(len(actions), 1, actions)
        self.assertFalse((base / "1").exists()); self.assertTrue((base / "2").exists()); self.assertTrue((base / "3").exists())
        self.assertEqual(sorted(zipfile.ZipFile(base / "archive" / "1.zip").namelist()), ["result.txt", "task_state/inbox.sqlite3"])

    def test_context_archive_zipped_by_month(self):
        chat = self.root / "callimachus" / "context_archive" / "123"
        touch(chat / "old1.json", 35, self.now); touch(chat / "old2.json", 36, self.now)   # same month
        touch(chat / "new.json", 3, self.now)
        actions = retention.sweep_context_archives(self.root, self.now)
        self.assertEqual(len(actions), 1)
        zips = list(chat.glob("*.zip")); self.assertEqual(len(zips), 1)
        self.assertEqual(sorted(zipfile.ZipFile(zips[0]).namelist()), ["old1.json", "old2.json"])
        self.assertTrue((chat / "new.json").exists())
        retention.sweep(self.root, self.now)
        self.assertTrue(retention.LOG.exists())


if __name__ == "__main__":
    unittest.main()
