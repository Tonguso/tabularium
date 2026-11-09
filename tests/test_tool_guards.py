"""The tool-call guards in execute_tool, on the real agent (rewritten 2026-10-14 from three lifted-function files).

- Identical calls: the third identical call in a row with an identical result is refused (task 82, 2026-10-07: the
  same invalid review_output ten times at ~160k tokens a turn). The per-turn budget line appended to results must not
  defeat this (Cato's review, 2026-10-07 21:12).
- Runaway: five consecutive failed or refused calls stop the worker with a diagnosis (Alex, 2026-10-07: an agent
  trapped in a loop must know when to stop).
- A tool's own exception comes back as an error result the model can act on (task 94, 2026-10-08), while worker
  control flow (TaskStopped) still propagates.
"""
import sys, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
from agent_harness import make_agent
from tabularium.core.outcome import Refused, Failed
from tabularium.records import background_runtime
class Guards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "coordinator"; root.mkdir()
        self.agent = make_agent("coordinator", root)
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def tool(self, results):
        """Replace the dispatcher below the guard with a scripted one."""
        it = iter(results) if not callable(results) else None

        def run(name, inputs, chat):
            self.calls.append((name, inputs))
            return results(name, inputs, chat) if it is None else next(it)
        self.agent._execute_tool_unguarded = run


class IdenticalCalls(Guards):
    def test_third_identical_call_with_identical_result_is_refused(self):
        self.tool(lambda *a: '{"status": "review_failed"}')
        args = ("review_output", {"path": "x.xlsx", "claims": ["a"]}, 1)
        self.agent.execute_tool(*args); self.agent.execute_tool(*args)
        self.assertIn("NOT executed", self.agent.execute_tool(*args))
        self.assertEqual(len(self.calls), 2)                                   # the third never ran
        self.agent.execute_tool("review_output", {"path": "x.xlsx"}, 1)         # changed inputs run again
        self.assertEqual(len(self.calls), 3)

    def test_identical_calls_with_changing_results_keep_running(self):
        n = iter(range(100))
        self.tool(lambda *a: f"status running, updated {next(n)}")
        for _ in range(5):
            self.assertNotIn("NOT executed", self.agent.execute_tool("background_task", {"action": "status", "task_id": 3}, 1))
        self.assertEqual(len(self.calls), 5)

    def test_changing_budget_line_does_not_hide_a_repeat(self):
        spent = iter(range(30000, 90000, 10000))
        self.tool(lambda *a: '{"status": "review_failed", "error": "e"}\n\n[Budget work] {"effective_spent": %d}' % next(spent))
        args = ("review_output", {"path": "x.xlsx", "claims": []}, 1)
        self.agent.execute_tool(*args); self.agent.execute_tool(*args)
        self.assertIn("NOT executed", self.agent.execute_tool(*args))


class Runaway(Guards):
    def test_five_consecutive_failures_stop_with_a_diagnosis(self):
        self.tool(lambda *a: Failed('{"status": "review_failed", "error": "x"}'))
        for i in range(4):                       # distinct inputs, so the identical-call guard does not fire first
            self.assertIn("review_failed", self.agent.execute_tool("review_output", {"i": i}, 7))
        with self.assertRaises(self.agent.RunawayStop) as ctx:
            self.agent.execute_tool("review_output", {"i": 4}, 7)
        self.assertIn("5 consecutive", str(ctx.exception))
        self.assertIn("review_output", str(ctx.exception))

    def test_identical_call_refusals_count_toward_the_stop(self):
        self.tool(lambda *a: Failed("Error: no such file"))
        args = ("read_file", {"path": "x"}, 1)
        for _ in range(4):
            self.agent.execute_tool(*args)                                      # two failures, two refusals
        with self.assertRaises(self.agent.RunawayStop):
            self.agent.execute_tool(*args)

    def test_one_success_resets_the_streak(self):
        self.tool([Failed("a"), Failed("b"), Failed("c"), Failed("d"), "saved", Failed("e"), Failed("f")])
        for i in range(7):
            self.agent.execute_tool("write_file", {"i": i}, 2)                  # never five in a row

    def test_what_counts_as_unproductive(self):
        for text in (Failed("Error: boom"), Refused("NOT executed: x"), Refused("I would rather not."), Failed("")):
            self.assertTrue(self.agent.unproductive(text), text)
        for text in ("saved D:\\x.xlsx 37 21", "Error: a bare string is a success; the kind is in the type", "",
                     "[Plan first] This tool call was NOT executed. Before any work, save task_checkpoint("):
            self.assertFalse(self.agent.unproductive(text), text)


class ToolExceptions(Guards):
    def test_a_raising_tool_returns_an_error_result(self):
        def boom(*a):
            raise ValueError("a plan needs done_when")
        self.tool(boom)
        out = self.agent.execute_tool("task_checkpoint", {"completed": "x"}, 1)
        self.assertTrue(out.startswith("Error: task_checkpoint failed: ValueError: a plan needs done_when"), out)
        self.assertTrue(self.agent.unproductive(out))                            # still counts toward the runaway stop

    def test_worker_control_flow_still_propagates(self):
        def cancelled(*a):
            raise background_runtime.TaskStopped()
        self.tool(cancelled)
        with self.assertRaises(background_runtime.TaskStopped):
            self.agent.execute_tool("web_search", {"query": "q"}, 1)


if __name__ == "__main__":
    unittest.main()
