"""A skill load shows the agent's earlier work with that skill (2026-10-21).

Cato designed a brand logo with logo-designer on 2026-10-03 (SVG build scripts, Alex's feedback round after
round). On 2026-10-21 he loaded the same skill for another logo, used an image model instead and was blocked twice.
Alex: [quote removed] Now the skill tool appends the agent's track record: real
tasks before chat turns, the most recent ones plus the first time (even from a task archived into a zip), never
diagnoses, trials or eval reruns, never the current work."""
import contextlib, io, json, sqlite3, tempfile, time, unittest, zipfile
from contextlib import closing
from pathlib import Path

from agent_harness import make_agent, put
from tabularium.records import skill_record
from tabularium.records.background_runtime import TaskStore
from tabularium.channels.owner_telegram import OWNER_CHAT_ID
CHAT = OWNER_CHAT_ID
DAY = 86400


def receipt(tool, inputs, job):
    return json.dumps({"job": job, "chat": CHAT, "tool": tool, "inputs": inputs, "status": "finished", "result": "ok"})


class SkillRecord(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "coordinator"
        self.root.mkdir()
        self.store = TaskStore(self.root)
        now = time.time()
        self.first = self.task("Redesign brand logo", now - 18 * DAY, ["build_logo_logo_concepts.py"], archive=True)
        self.old = self.task("Logo colour study", now - 10 * DAY, ["colours.svg"])
        self.recent = self.task("Badge logo in SVG", now - 1 * DAY, ["Badge.svg"])
        self.meta = self.task("[Correction diagnosis] Alex corrected you", now - 0.5 * DAY, [])
        self.recall = self.root / "task_state" / "conversation_recall.sqlite3"
        from tabularium.memory.conversation_recall import ConversationRecall
        ConversationRecall(self.root)                                     # the real archive schema
        with closing(sqlite3.connect(self.recall)) as db, db:
            said = time.strftime("%Y-%m-%dT%H:%M", time.localtime(now - 18 * DAY + 3600))
            db.execute("INSERT INTO messages(fingerprint,chat,speaker,said_at,text) VALUES ('f1',?,'user',?,?)",
                       (str(CHAT), said, "[2026-10-03 Saturday 12:02] 用印篆体"))

    def task(self, request, at, files, archive=False):
        ident = self.store.start(CHAT, request)["id"]
        put(self.store, ident, "finished")
        with self.store.db() as db:
            db.execute("UPDATE tasks SET updated=? WHERE id=?", (at + 600, ident))
        records = [(int(at * 1e9), receipt("skill", {"name": "logo-designer"}, ident))]
        records += [(int((at + 60 + i) * 1e9), receipt("write_file", {"path": f"D:/x/{f}", "content": "<svg/>"}, ident))
                    for i, f in enumerate(files)]
        folder = self.store.directory(ident) / "task_state" / "actions"
        if archive:
            zpath = self.root / "task_state" / "background" / "archive" / f"{ident}.zip"
            zpath.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(zpath, "w") as z:
                for ns, body in records:
                    z.writestr(f"task_state/actions/{ns}.json", body)
        else:
            folder.mkdir(parents=True, exist_ok=True)
            for ns, body in records:
                (folder / f"{ns}.json").write_text(body, encoding="utf-8")
        return ident

    def test_backfill_finds_live_and_archived_uses_once(self):
        self.assertEqual(skill_record.backfill(self.root), 4)
        self.assertEqual(skill_record.backfill(self.root), 0)          # rerunning adds nothing

    def test_the_record_shows_recent_work_and_the_first_time_never_meta_or_current(self):
        skill_record.backfill(self.root)
        out = skill_record.record(self.root, "logo-designer", CHAT, "task", self.recent, self.store, None, limit=3)
        self.assertNotIn("Badge", out)                                 # the current work is not its own precedent
        self.assertNotIn("Correction diagnosis", out)
        self.assertIn("Logo colour study", out)
        self.assertIn(f"task {self.first}, the first time you used this skill", out)
        self.assertIn("build_logo_logo_concepts.py", out)           # read from the zipped task
        self.assertIn("用印篆体", out)                                 # his reaction, timestamp prefix removed
        self.assertNotIn("[2026-10-03", out)
        self.assertLess(len(out), 1200)

    def test_no_history_adds_nothing(self):
        self.assertEqual(skill_record.record(self.root, "xlsx", CHAT, "turn", 1, self.store, None), "")

    def test_the_skill_tool_appends_the_record_and_notes_the_load(self):
        with contextlib.redirect_stdout(io.StringIO()):
            agent = make_agent("coordinator", self.root)
        agent.BACKGROUND_TASK_ID = ""
        agent._background = self.store
        skill_record.backfill(self.root)
        folder = self.root / "skills" / "logo-designer"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text("---\nname: logo-designer\ndescription: Design logos in SVG.\n---\n"
                                         "# Logo Designer\nDesign and iterate on logos using SVG.\n", encoding="utf-8")
        skill = "logo-designer"
        skill_record.note(self.root, skill, "task", self.first, CHAT)
        agent._inbox.local.job = 777
        text = agent.execute_tool("skill", {"name": skill}, CHAT)
        self.assertIn("# Skill: " + skill, text)
        self.assertIn("Your earlier work with this skill", text)
        with closing(sqlite3.connect(self.root / "task_state" / "skill_uses.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM uses WHERE skill=? AND scope='turn' AND ref=777",
                                        (skill,)).fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
