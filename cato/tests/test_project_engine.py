"""Offline checks for actual persistence/status failures; no model calls or live actions."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT.parent)]
from agent_core.project_store import ProjectStore
from background_runtime import TaskStore
from agent_core.conversation_recall import query_terms

class ProjectEngine(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.store=ProjectStore(self.root)
    def tearDown(self):
        self.tmp.cleanup()
    def project(self):
        return dict(title="Example",goal="Reconcile",next_action="Check values",evidence=[],
                    completion_criteria=["Totals agree"])
    def test_version_conflicts_and_history(self):
        data=self.project()
        self.store.manage(1,"save",key="example",data=data)
        with self.assertRaises(ValueError):
            self.store.manage(1,"save",key="example",data=data)
        data["next_action"]="Inspect source"
        self.store.manage(1,"save",key="example",data=data,version=1)
        self.assertEqual(len(self.store.manage(1,"history",key="example")),1)
        self.assertEqual(self.store.manage(1,"read",key="example")["version"],2)
    def test_deadline_alert_deduplicates(self):
        data=self.project(); data["deadline"]="2020-01-01T12:00:00+00:00"
        self.store.manage(1,"save",key="example",data=data)
        tasks=TaskStore(self.root); alerts=[]
        self.store.monitor(lambda *x:alerts.append(x),tasks)
        self.store.monitor(lambda *x:alerts.append(x),tasks)
        self.assertEqual(len(alerts),1)
    def test_no_lesson_self_promotion(self):
        data=dict(title="Lesson",failure="Bad state",source="record",candidate_change="Check lease",
                  checks=[],limitations="Not evaluated",status="verified")
        with self.assertRaises(ValueError):
            self.store.manage(1,"save",kind="lesson",key="example",data=data)
    def test_checkpoint_persists_and_missing_result_is_not_claimed(self):
        tasks=TaskStore(self.root)
        t=tasks.start(1,"Read research")
        tasks.checkpoint(1,t["id"],"Read first source","Read second",["source.txt"])
        self.assertIsNone(tasks.status(1)[0]["result_file"])
        self.assertTrue((tasks.directory(t["id"])/"checkpoint.json").exists())
        with self.assertRaises(ValueError):
            tasks.evidence(2,t["id"])
    def crash(self, tasks, ident):
        tasks.move(ident, "stop", "interrupted", "Worker error: RuntimeError. Inspect worker.log and saved actions before resuming.")
        with tasks.db() as db:
            db.execute("UPDATE tasks SET updated=0")
    def test_crashed_worker_with_clean_records_is_rerun_at_most_twice(self):
        tasks=TaskStore(self.root); t=tasks.start(1,"Read research")
        for n in (1,2):
            self.crash(tasks,t["id"]); tasks.retry_interrupted()
            self.assertEqual(tasks.get(t["id"])["status"],"queued",n)
            self.assertIn("Recovery queued",tasks.get(t["id"])["note"])
        self.crash(tasks,t["id"]); tasks.retry_interrupted()
        self.assertEqual(tasks.get(t["id"])["status"],"interrupted")
        self.assertEqual(json.loads((tasks.directory(t["id"])/"context.json").read_text())["retries"],2)
    def test_unfinished_external_action_blocks_automatic_rerun(self):
        tasks=TaskStore(self.root); t=tasks.start(1,"Send the report")
        actions=tasks.directory(t["id"])/"task_state"/"actions"; actions.mkdir(parents=True)
        (actions/"1.json").write_text(json.dumps({"tool":"send_email","status":"started"}),encoding="utf-8")
        self.crash(tasks,t["id"]); tasks.retry_interrupted()
        self.assertEqual(tasks.get(t["id"])["status"],"interrupted")
        self.assertIn("send_email started and never reported",tasks.get(t["id"])["note"])
    def test_stops_that_are_not_crashes_are_never_rerun(self):
        tasks=TaskStore(self.root); t=tasks.start(1,"Looping task")
        tasks.move(t["id"],"stop","interrupted","Stopped: repeated failed or refused tool calls; see the result.")
        with tasks.db() as db:
            db.execute("UPDATE tasks SET updated=0")
        tasks.retry_interrupted()
        self.assertEqual(tasks.get(t["id"])["status"],"interrupted")
    def test_start_has_no_mode_and_any_brief_is_accepted(self):
        tasks=TaskStore(self.root)
        self.assertEqual(tasks.start(1,"Read only. Do not delegate; no Cal or Vit calls.")["status"],"queued")
        self.assertEqual(tasks.start(1,"Brazil election note; get Cal to challenge it")["status"],"queued")
        self.assertNotIn("warning",tasks.control(1,tasks.start(1,"Read research")["id"],"steer","Add a Cal review"))
    def test_bilingual_retrieval(self):
        self.assertIn("工作邮箱",query_terms("work email"))
        self.assertIn("work email",query_terms("工作邮箱"))
