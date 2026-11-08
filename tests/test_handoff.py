"""A hand-off to Cal or Vit from the chat needs no worker slot (2026-10-21).

22:19 Alex: [quote removed] Cato had queued a Cato background task
whose job was to submit the Tiro fixes to Vit; it waited behind the fund-list task for the single worker slot, so
Vit received nothing. A foreground submit now starts the delegate at once and creates the task that will receive
and deliver the result already parked; it wakes, and only then takes a slot, when the result returns."""
import contextlib, io, json, tempfile, time, unittest
from pathlib import Path
from types import SimpleNamespace as NS

from agent_harness import make_agent, put
from tabularium.records.background_runtime import WAITING
CHAT = 1


class Handoff(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / "cato"; root.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            self.agent = make_agent("cato", root)
        self.agent.BACKGROUND_TASK_ID = ""
        self.store = self.agent._background
        self.store.worker_limit = lambda: 1
        for t in self.store.status(CHAT):                                # the harness's own self-test task
            put(self.store, t["id"], "finished")
        self.sent = []
        def runner(argv):
            self.sent.append(argv)
            return NS(returncode=0, stdout=json.dumps({"status": "queued", "request_id": "r"}), stderr="")
        self.runner = runner

    def submit(self):
        items = [{"item": "selected subtitle is the default track", "check": "ffprobe flags", "critical": True, "early": True},
                 {"item": "font size scales with the video", "check": "render at two sizes", "critical": True}]
        return self.agent.tool_delegate(action="submit", chat_id=CHAT, agent="vit", brief="Fix Tiro: the chosen subtitle must be the default; "
                                        "the recommended font size must scale with the video.", purpose="Tiro fixes",
                                        completion_items=items, completion_condition="Both defects fixed and verified",
                                        budget_tokens=50000, allowed_paths=[str(Path(self.tmp.name) / "tiro")], _runner=self.runner)

    def test_the_delegate_starts_now_and_the_owner_waits_without_a_slot(self):
        self.store.start(CHAT, "fund-list expansion")        # the single slot is taken
        self.store.claim_next()
        t0 = time.time()
        out = json.loads(self.submit())
        self.assertEqual(len(self.sent), 1)                                 # Vit was called at once
        self.assertIn("without holding a worker slot", out["owner_task"])
        owner = [t for t in self.store.status(CHAT) if t["status"] == WAITING][0]
        self.assertEqual(owner["status"], WAITING)
        self.assertTrue(owner["request"].startswith("Receive Vit's work for Alex: Tiro fixes"))
        self.assertEqual([d["status"] for d in self.store.delegations(owner["id"])], ["submitted"])
        self.assertTrue(self.store.activated_since(CHAT, t0))               # "work started" is backed
        self.assertIsNone(self.store.claim_next())                          # nothing new was queued for the slot

    def test_the_owner_wakes_when_the_result_returns(self):
        json.loads(self.submit())
        owner = self.store.status(CHAT)[0]
        request_id = self.store.delegations(owner["id"])[0]["request_id"]
        self.store.status_checker = lambda agent, rid: "completed"
        self.store.poll_waiting()
        self.assertEqual(self.store.get(owner["id"])["status"], "queued")
        steer = self.store.pending_controls(owner["id"])
        self.assertTrue(steer and request_id in steer[0]["text"] and "delegate(action='result'" in steer[0]["text"])
        self.assertEqual(self.store.claim_next()["id"], owner["id"])        # now it takes a slot, to accept and deliver


if __name__ == "__main__":
    unittest.main()
