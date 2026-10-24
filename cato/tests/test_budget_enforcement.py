"""Offline: effective-token measurement (task_cost), delegation contracts, and the
no-poll gaps closed on 2026-10-06 (read_file, relative paths, narrow wait regex, durable status timer)."""
import ast
from contextlib import closing
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
import task_cost as pb
from agent_core.roster import ROLES
from background_runtime import (TaskStore, IN_FLIGHT_REFUSAL, REMOTE_RETURNED, DELEGATE_MAX_WAIT_SECONDS,

                              WAIT_COMMAND_RE)


def add_usage(directory, kind="response", **metrics):
    path = Path(directory) / "task_state" / "inbox.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS usage_events (id INTEGER PRIMARY KEY, job INTEGER, created REAL, kind TEXT, provider TEXT, model TEXT, billing TEXT, role TEXT, metrics TEXT)")
        db.execute("INSERT INTO usage_events(job,created,kind,provider,model,billing,role,metrics) VALUES (1,0,?,'anthropic','m','api','worker',?)",
                   (kind, json.dumps(metrics)))


def progress(directory):
    """One successful action record dated after any budget revision: what an extension now requires."""
    import time as _t
    folder = Path(directory) / "task_state" / "actions"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{_t.time_ns()}.json"
    path.write_text(json.dumps({"tool": "write_file", "status": "finished", "result": "saved"}), encoding="utf-8")
    os.utime(path, (_t.time() + 1, _t.time() + 1))


class TaskCost(unittest.TestCase):
    """task_cost (2026-10-09): measurement and one awareness line; nothing stops work."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.task = self.root / "cal" / "task_state" / "background" / "3"
        self.task.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _contract(self, items=None, cap=5000):
        (self.task / "context.json").write_text(json.dumps({"request": "r", "contract": {
            "completion_items": items or [{"item": "delisting dates", "check": "filing", "critical": True},
                                          {"item": "segment shares", "check": "report", "critical": True},
                                          {"item": "nice chart", "check": "file", "critical": False}],
            "budget_effective_tokens": cap}}), encoding="utf-8")

    def test_effective_weights(self):
        self.assertEqual(pb.effective_tokens({"input_uncached": 1000, "cache_read": 10000, "cache_write": 0, "output": 100}), 2500)
        self.assertEqual(pb.effective_tokens({"input_total": 11000, "cache_read": 10000, "output": 0}), 2000)

    def test_foreground_has_no_line(self):
        self.assertIsNone(pb.phase(self.task))
        self.assertEqual(pb.notice(self.task), "")

    def test_delegate_target_is_information(self):
        self._contract(cap=2000)
        self.assertEqual(pb.phase(self.task), "work")
        add_usage(self.task, input_uncached=1000, cache_read=10000, cache_write=0, output=100)   # 2500 effective > 2000
        self.assertEqual(pb.phase(self.task), "over_target")
        for _ in range(3):
            add_usage(self.task, kind="tool_started")
        text = pb.notice(self.task)
        self.assertIn("[Budget over_target]", text)
        self.assertIn('"brief_target": 2000', text)
        self.assertIn("do not stop for the number", text)
        self.assertEqual(pb.set_target(self.task, 20000), 20000)                 # any target, no approval flag
        self.assertEqual(pb.target(self.task), 20000)
        self.assertEqual(pb.phase(self.task), "work")                            # projected 10,000 (2,500 + 3 pending turns) < 20,000
        self.assertEqual(pb.status(self.task)["target"], 20000)

    def test_owner_estimate_is_awareness_only(self):
        owner = self.root / "cato" / "task_state" / "background" / "9"
        owner.mkdir(parents=True)
        (owner / "context.json").write_text(json.dumps({"request": "r"}), encoding="utf-8")
        self.assertEqual(pb.phase(owner), "work")
        (owner / "checkpoint.json").write_text(json.dumps({"estimate_effective_tokens": 10000, "estimate_turns": 2}), encoding="utf-8")
        self.assertIn('"own_estimate": 10000', pb.notice(owner))
        for _ in range(5):
            add_usage(owner, input_uncached=4000, output=100)                   # 22,500 > 2 x 10,000
        self.assertEqual(pb.phase(owner), "over_estimate")
        self.assertIn("twice your own estimate", pb.notice(owner))

    def test_budget_does_not_repeat_requirement_context(self):
        self._contract(cap=5000)
        add_usage(self.task, input_uncached=1000, cache_read=0, cache_write=0, output=0)
        (self.task / "checkpoint.json").write_text(json.dumps({"estimate_effective_tokens": 4000, "items_done": [1]}), encoding="utf-8")
        text = pb.notice(self.task)
        self.assertNotIn("segment shares", text)
        self.assertNotIn("1. delisting", text)



class NoPollGaps(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_file_refused_for_other_agent_state_including_relative(self):
        self.store.add_delegation(7, "cal", "req-1", "research", "worker_judgment")
        for path in [r"D:\Agents\callimachus\task_state\background\3\checkpoint.json",
                     r"..\callimachus\task_state\background\3\checkpoint.json",
                     "../vitruvius/health.json", r"D:\Agents\vitruvius\task_state\transport.json"]:
            self.assertEqual(self.store.path_refusal(7, path), IN_FLIGHT_REFUSAL, path)
        for path in [r"D:\Agents\cato\task_state\background\7\checkpoint.json", "task_state/background/7/checkpoint.json",
                     r"D:\Agents\callimachus\reports\summary.md", r"D:\projects\mylib\pkg\main.py"]:
            self.assertIsNone(self.store.path_refusal(7, path), path)
        self.assertIsNone(self.store.path_refusal(8, r"D:\Agents\callimachus\task_state\x.json"))  # not in flight

    def test_wait_regex_is_narrow(self):
        self.store.add_delegation(7, "vit", "req-2", "build", "worker_judgment")
        for cmd in ["python scripts/sleep_study.py", "Get-Content ping_check.log", "python timeout_report.py",
                    "gc ..\\cato\\task_state\\background\\7\\checkpoint.json"]:
            self.assertIsNone(self.store.command_refusal(7, cmd), cmd)
        for cmd in ["Start-Sleep 55", "sleep 30", "ping -n 60 127.0.0.1", "timeout /t 60",
                    "gc ..\\callimachus\\task_state\\background\\3\\checkpoint.json"]:
            self.assertEqual(self.store.command_refusal(7, cmd), IN_FLIGHT_REFUSAL, cmd)
        self.assertFalse(WAIT_COMMAND_RE.search("python sleep_study.py"))

    def test_status_timer_survives_restart(self):
        self.store.add_delegation(7, "cal", "req-1", "research", "worker_judgment")
        t0 = 1_000_000.0
        self.assertIsNone(self.store.status_refusal(7, "req-1", now=t0))
        fresh = TaskStore(Path(self.tmp.name))   # a resumed worker builds a new store
        self.assertEqual(fresh.status_refusal(7, "req-1", now=t0 + 60), IN_FLIGHT_REFUSAL)
        self.assertIsNone(fresh.status_refusal(7, "req-1", now=t0 + 301))

    def test_capped_is_a_returned_state_and_wait_recalibrated(self):
        self.assertIn("capped", REMOTE_RETURNED)
        self.assertEqual(DELEGATE_MAX_WAIT_SECONDS, 45 * 60)
        self.store.add_delegation(7, "cal", "req-1", "research", "worker_judgment")
        self.store.mark_delegation(7, "req-1", "returned")
        self.store.reopen_delegation(7, "req-1")
        self.assertEqual(self.store.delegations(7)[0]["status"], "submitted")

    def test_acceptance_is_what_marks_a_delegation_read(self):
        items = [{"item": "a", "check": "c", "critical": True}, {"item": "b", "check": "c", "critical": False}]
        self.store.add_delegation(7, "cal", "req-9", "research", "worker_judgment")
        self.store.record_submission("req-9", 7, "cal", "research", 250000, items)
        self.assertEqual(self.store.contract_items("req-9"), items)
        self.store.mark_delegation(7, "req-9", "returned")
        self.assertTrue(self.store.unread_delegations(7))                      # fetched but not accepted: still blocks
        critical_unmet, unmet = self.store.accept_delegation(7, "req-9", [
            {"item": 1, "verdict": "met", "evidence": "recomputed from filing"},
            {"item": 2, "verdict": "partial", "evidence": "file exists, chart empty"}])
        self.assertEqual((critical_unmet, unmet), ([1], [1, 2]))  # reviewing reports is not proof
        self.assertFalse(self.store.unread_delegations(7))
        card = self.store.scorecard("req-9")
        self.assertEqual((card["items_total"], card["items_met"], card["critical_total"], card["critical_met"], card["reworks"]), (2, 0, 1, 0, 0))
        self.store.bump_rework("req-9")
        self.assertEqual(self.store.scorecard("req-9")["reworks"], 1)
        self.assertIn("accept", self.store.delivery_block(8) or "accept")      # no delegations on 8: None is fine

    def test_checkpoint_keeps_estimate_and_items_done(self):
        with self.store.db() as db:
            db.execute("INSERT INTO tasks(id,chat,request,status,created,updated) VALUES (7,1,'r','running',0,0)")
        self.store.directory(7).mkdir(parents=True, exist_ok=True)
        partial = self.store.checkpoint(1, 7, "half", "rest", ["x"], estimate_effective_tokens=4000)   # estimate without a plan: fine
        self.assertNotIn("note", partial)
        first = self.store.checkpoint(1, 7, "half", "rest", ["x"], estimate_effective_tokens=4000, items_done=[1],
                                      plan=["a", "b"], estimate_turns=3, done_when="file exists", task_type="probe")
        self.assertNotIn("safety_net", first)                                   # no net, no allowance (2026-10-08)
        second = self.store.checkpoint(1, 7, "more", "rest", ["y"])
        second = pb.checkpoint(self.store.directory(7))
        self.assertEqual((second["estimate_effective_tokens"], second["items_done"], second["plan"], second["done_when"]),
                         (4000, [1], ["a", "b"], "file exists"))
        ledger = self.store.checkpoint(1, 7, "more", "rest", ["y"], facts=["AVGO AI revenue 56% (Q3 FY26 release)"],
                                       guesses=["LITE AI share > 50%"], decisions=["Alex: \"six names only\"", "Cato: FY ends June for LITE"])
        ledger = pb.checkpoint(self.store.directory(7))
        self.assertEqual(ledger["decisions"][0], "Alex: \"six names only\"")
        kept = self.store.checkpoint(1, 7, "more", "rest", ["y"], guesses=[])                 # empty list: previous kept
        kept = pb.checkpoint(self.store.directory(7))
        self.assertEqual((kept["facts"], kept["guesses"], len(kept["decisions"])), (ledger["facts"], ledger["guesses"], 2))
        third = self.store.checkpoint(1, 7, "done", "", ["z"], items_done=[1, 2], lesson="size by turns, not by hope")
        self.assertEqual(pb.checkpoint(self.store.directory(7))["items_done"], [1, 2])
        from agent_core.project_store import ProjectStore
        record = ProjectStore(self.tmp.name).manage(1, 'read', 'lesson', third['candidate_lesson'])
        self.assertIn("size by turns", record['data']['candidate_change'])
        self.assertEqual(record['data']['status'], 'candidate')
        self.assertEqual(self.store.lessons(1, 'research'), '')

    def test_loaded_skill_output_contract_stays_in_the_working_request(self):
        import background_runtime as rt
        with self.store.db() as db:
            db.execute("INSERT INTO tasks(id,chat,request,status,created,updated) VALUES (9,1,'r','running',0,0)")
        actions = self.store.directory(9) / "task_state" / "actions"; actions.mkdir(parents=True)
        (actions / "1.json").write_text(json.dumps({"tool": "skill", "status": "finished", "inputs": {"name": "basket"}}), encoding="utf-8")
        (actions / "2.json").write_text(json.dumps({"tool": "skill", "status": "finished", "inputs": {"name": "plain"}}), encoding="utf-8")
        body = "# Basket\n\n## Step 1\nlook\n\n## Output\nKeep sections I-IV.\nGrade exposure High/Medium/Low.\n\n## Notes\nx"
        class Agent:
            turns = []
            def discover_skills(self):
                return {"basket": {"body": body}, "plain": {"body": "# Plain\nno output section"}}
            def save_conversation(self, chat): pass
            def run_turn(self, messages, chat):
                Agent.turns.append(messages[-1]["content"]); return "corrected answer"
        contracts = rt.skill_output_contracts(self.store, 9, Agent())
        self.assertEqual([c[0] for c in contracts], ["basket"])                    # only skills with an Output section
        self.assertTrue(contracts[0][1].startswith("## Output") and "Grade exposure" in contracts[0][1])
        self.assertNotIn("## Notes", contracts[0][1])
        from engine import AgentRuntime
        runtime = AgentRuntime()
        runtime._background = self.store
        runtime.BACKGROUND_TASK_ID = "9"
        runtime.discover_skills = Agent().discover_skills
        wire = runtime._pin_contract([{"role": "user", "content": "seed"}])
        self.assertIn("Grade exposure High/Medium/Low", str(wire))
        self.assertEqual(Agent.turns, [])

    def test_delegate_question_is_relayed_once_and_wakes_a_parked_task(self):
        import background_runtime as rt
        with self.store.db() as db:
            db.execute("INSERT INTO tasks(id,chat,request,status,created,updated) VALUES (11,1,'r',?,0,0)", (rt.WAITING,))
        folder = self.store.directory(11); folder.mkdir(parents=True, exist_ok=True)
        (folder / "context.json").write_text(json.dumps({"context": "", "parked_at": 0}), encoding="utf-8")
        self.store.add_delegation(11, "cal", "cato-cal-11-x", "exposure facts", "worker_judgment")
        answers = {"cato-cal-11-x": {"status": "question", "question": "Which fiscal year: FY25 or FY26?"}}
        self.store.status_checker = lambda agent, rid: answers.get(rid)
        in_flight = self.store.refresh_delegations(11)
        self.assertEqual([d["request_id"] for d in in_flight], ["cato-cal-11-x"])          # still in flight
        controls = self.store.pending_controls(11)
        self.assertEqual(len(controls), 1)
        self.assertIn("Which fiscal year", controls[0]["text"])
        self.assertIn("delegate(action='answer'", controls[0]["text"])
        self.store.refresh_delegations(11)
        self.assertEqual(len(self.store.pending_controls(11)), 1)                           # relayed once, not per poll
        self.assertTrue(self.store.unanswered(11, in_flight))
        self.store.poll_waiting()
        self.assertEqual(self.store.get(11)["status"], "queued")                            # woken to answer
        self.store.acknowledge([controls[0]["id"]])                                         # the worker read it
        self.assertFalse(self.store.unanswered(11, in_flight))
        answers["cato-cal-11-x"] = "completed"
        self.assertEqual(self.store.refresh_delegations(11), [])                             # returned afterwards
        self.assertEqual(self.store.delegations(11)[0]["status"], "returned")

    def test_open_question_reads_the_checkpoint_blocker(self):
        with self.store.db() as db:
            db.execute("INSERT INTO tasks(id,chat,request,status,created,updated) VALUES (12,1,'r','running',0,0)")
        folder = self.store.directory(12); folder.mkdir(parents=True, exist_ok=True)
        self.store.checkpoint(1, 12, "half", "wait", [], blocker="QUESTION: use adjusted or raw prices?")
        self.assertEqual(self.store.open_question(12), "use adjusted or raw prices?")
        self.store.checkpoint(1, 12, "half", "go", [], blocker="")
        self.assertEqual(self.store.open_question(12), "")
        src = (ROOT.parent / "background_runtime.py").read_text(encoding="utf-8")
        self.assertIn("store.finish(ident, outcome)", src)                                 # a delegate stops to wait, not finish
        for client in (ROOT.parent / "callimachus" / "research_client.py", ROOT.parent / "vitruvius" / "work_client.py"):
            text = client.read_text(encoding="utf-8")
            self.assertIn("action=='answer'", text, client)
            self.assertIn("'needs_answer':'question'", text, client)
            self.assertIn("output['checkpoint']=checkpoint.read_text", text, client)
            self.assertIn("DONE: <item numbers met>", text, client)

    def test_cal_runs_two_workers_others_one(self):
        with self.store.db() as db:
            for i in (31, 32, 33):
                db.execute("INSERT INTO tasks(id,chat,request,status,created,updated) VALUES (?,1,'r','queued',0,0)", (i,))
        self.assertEqual(self.store.worker_limit(), 1)                                   # temp folder: Cato behaviour
        self.assertEqual(self.store.claim_next()["id"], 31)
        self.assertIsNone(self.store.claim_next())                                       # one slot, taken
        self.store.role = ROLES["callimachus"]
        self.assertEqual(self.store.worker_limit(), 2)
        self.assertEqual(self.store.claim_next()["id"], 32)                              # second slot
        self.assertIsNone(self.store.claim_next())                                       # third queues
        with self.store.db() as db:
            db.execute("UPDATE tasks SET status='finished' WHERE id=31")
        self.assertEqual(self.store.claim_next()["id"], 33)

    def test_register_lessons_reach_the_worker(self):
        import sqlite3 as _sq
        (Path(self.tmp.name) / "lessons.md").write_text("- 2026-10-08 task 1: one-line lesson\n", encoding="utf-8")
        reg = Path(self.tmp.name) / "task_state" / "projects.sqlite3"; reg.parent.mkdir(parents=True, exist_ok=True)
        with closing(_sq.connect(reg)) as con:                      # the store already created the register's tables
            con.execute("INSERT INTO records(chat,kind,key,version,data,updated) VALUES ('1','lesson','size-from-route',1,?,2.0)",
                        (json.dumps({"title": "Size Cal budgets from the active route", "candidate_change": "read usage_events per route before sizing"}),))
            con.execute("INSERT INTO records(chat,kind,key,version,data,updated) VALUES ('1','project','p',1,'{}',3.0)")
            con.commit()
        text = self.store.lessons(1, 'research')
        self.assertEqual(text, '')  # historical/candidate lessons are evidence, not instructions


class DelegateContract(unittest.TestCase):
    """The delegate tool on the real Cato agent: a submission needs a definition of done; a result counts as read only
    when accepted with a verdict per item (rewritten 2026-10-14 from source-string checks)."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "cato"; root.mkdir()
        sys.path.insert(0, str(ROOT.parent / "tests"))
        from agent_harness import make_agent
        self.agent = make_agent("cato", root)
        self.never = lambda argv: self.fail("refused calls must not reach the delegate script")

    def tearDown(self):
        self.tmp.cleanup()

    def test_submit_needs_a_brief_items_and_a_definition_of_done(self):
        d = self.agent.tool_delegate
        self.assertIn("self-contained brief", d(action="submit", chat_id=1, agent="cal", _runner=self.never))
        items = [{"item": "facts for 8 names", "check": "open 2 URLs", "critical": True, "early": True}]
        out = d(action="submit", chat_id=1, agent="cal", brief="Research X", purpose="for a basket",
                completion_items=items, _runner=self.never)
        self.assertIn("completion_condition", out)

    def test_accept_needs_a_verdict_for_every_critical_item(self):
        out = self.agent.tool_delegate(action="accept", chat_id=1, agent="cal", request_id="req-1", verdicts=[], _runner=self.never)
        self.assertNotIn('"accepted": true', out.lower())


class Guards(unittest.TestCase):
    """read_file in a worker consults the no-poll path rule; workers get the smaller request budget."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "cato"; root.mkdir()
        sys.path.insert(0, str(ROOT.parent / "tests"))
        from agent_harness import make_agent
        self.agent = make_agent("cato", root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_worker_read_file_is_checked(self):
        self.agent.BACKGROUND_TASK_ID = "1"
        self.agent._background.path_refusal = lambda ident, path: "REFUSED " + path
        self.assertEqual(self.agent.tool_read_file(r"D:\Agents\callimachus\task_state\health.json"),
                         r"REFUSED D:\Agents\callimachus\task_state\health.json")

    def test_worker_context_budget_is_smaller(self):
        seen = []
        g = self.agent._prepare_request_context.__func__.__globals__
        original = g["prepare_context"]
        g["prepare_context"] = lambda messages, directory, budget_bytes: seen.append(budget_bytes) or messages
        try:
            self.agent.BACKGROUND_TASK_ID = "1"; self.agent._prepare_request_context([], 1)
            self.agent.BACKGROUND_TASK_ID = ""; self.agent._prepare_request_context([], 1)
        finally:
            g["prepare_context"] = original
        self.assertLess(seen[0], seen[1])


class BriefRules(unittest.TestCase):
    """delegation_contract: the parts of a clear brief that are arithmetic, not prose."""
    def setUp(self):
        import delegation_contract as dc
        self.dc = dc
        self.items = [{"item": "facts for 8 tier-one names", "check": "open 2 URLs", "critical": True, "early": True},
                      {"item": "dated policy timeline", "check": "open 2 URLs", "critical": True},
                      {"item": "chart", "check": "file exists", "critical": False}]

    def test_accept_needs_every_critical_verdict_and_tolerates_missing_non_critical(self):
        ok = [{"item": 1, "verdict": "met", "evidence": "opened two IR URLs and recomputed one share", "how": "read"},
              {"item": 2, "verdict": "partial", "evidence": "timeline has 5 of 8 dated entries, checked against MIIT", "how": "read"}]
        self.assertIn("needs how", self.dc.validate_verdicts([dict(ok[0], how=None), ok[1]], self.items))   # 2026-10-23
        self.assertIsNone(self.dc.validate_verdicts(ok, self.items))                        # item 3 (non-critical) left out
        self.assertIn("missing: [2]", self.dc.validate_verdicts(ok[:1], self.items))
        self.assertIn("critical", self.dc.validate_verdicts(ok[:1], self.items))

    def test_items_limits_and_early_slice(self):
        self.assertIsNone(self.dc.validate_items(self.items))
        no_early = [dict(self.items[0], early=False)] + self.items[1:]
        self.assertIn("early judgment slice", self.dc.validate_items(no_early))
        too_many = self.items + [{"item": f"x{i}", "check": "c", "critical": False} for i in range(4)]
        self.assertIn("several jobs in one", self.dc.validate_items(too_many))
        four_critical = [dict(it, critical=True) for it in self.items] + [{"item": "d", "check": "c", "critical": True}]
        self.assertIn("several jobs in one", self.dc.validate_items(four_critical))


    def test_card_paths_are_enforced(self):
        card = {"writable_paths": [r"D:\Agents\callimachus\reports", r"D:\Agents\callimachus\scratch"]}
        self.assertIsNone(self.dc.path_refusal("cal", card, r"D:\Agents\callimachus\reports\pack.md", "brief"))
        self.assertIn("cannot write", self.dc.path_refusal("cal", card, r"D:\Agents\cato\scratch\pack.md", "brief"))
        brief = r"Returns a markdown pack saved to D:\Agents\cato\scratch\jp_food_security_cal.md with a table."
        self.assertIn("cannot write", self.dc.path_refusal("cal", card, "", brief))
        reads = r"Read the draft list in D:\Agents\cato\reports\draft.md and verify each code."   # reading is fine
        self.assertIsNone(self.dc.path_refusal("cal", card, "", reads))
        self.assertIsNone(self.dc.path_refusal("cal", {}, r"D:\anywhere", "x"))                       # no card: no rule
        for agent in ("cal", "vit"):
            self.assertTrue(self.dc.load_card(agent).get("writable_paths"), agent)



    def test_exemplar_and_lesson_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.dc.BRIEFS = Path(tmp)
            row = {"request_id": "req-1", "agent": "cal", "task_type": "research", "budget": 300000, "spent": 210000, "items_met": 3,
                   "items_total": 3, "reworks": 0, "items": json.dumps(self.items), "brief": json.dumps({"title": "T", "question": "Q", "sizing": {"units": 8}})}
            path = self.dc.save_exemplar(row)
            text = path.read_text(encoding="utf-8")
            self.assertIn("[critical] [early] facts for 8", text); self.assertIn("## Brief", text)
            lesson = self.dc.append_lesson("req-2", "cal", "Should have tiered the names and sized 8 not 50.", [1])
            self.assertIn("tiered the names", lesson.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
