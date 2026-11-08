"""A ChatGPT-plan stream that ends badly hands the turn to the next route instead of killing it (real agent).
2026-10-07 08:57: a dropped stream raised a bare RuntimeError, which provider routing treats as 'needs attention, no
replay', so a foreground turn and a worker died with the Claude route idle. 14:13: 'servers overloaded' arrived as a
bare APIError (no status code) mid-stream and killed task 80. Rewritten 2026-10-14 from a source-inspection test."""
import sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace as NS
ROOT = Path(__file__).resolve().parents[1]
from agent_harness import make_agent
from tabularium.models import provider_routing
class Stream:
    def __init__(self, events=(), fail=None):
        self.events, self.fail = list(events), fail

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for e in self.events:
            yield e
        if self.fail:
            raise self.fail


class StreamEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name) / "tacitus"; root.mkdir()
        cls.agent = make_agent("tacitus", root)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def call(self, stream):
        client = NS(responses=NS(create=lambda **kw: stream))
        return self.agent._responses_call(client, model="m", instructions="", input_items=[], tools=[], plan_mode=True)

    def test_stream_without_completion_steps_aside(self):
        with self.assertRaises(provider_routing.Unavailable) as ctx:
            self.call(Stream([NS(type="response.output_text.delta", delta="half an answer")]))
        self.assertIn("without a completed response", str(ctx.exception))

    def test_mid_stream_error_without_a_status_steps_aside(self):
        with self.assertRaises(provider_routing.Unavailable) as ctx:
            self.call(Stream([NS(type="response.created")], fail=Exception("Our servers are currently overloaded")))
        self.assertIn("failed before completion", str(ctx.exception))

    def test_errors_with_a_status_keep_their_own_handling(self):
        err = Exception("unauthorised"); err.status_code = 401
        with self.assertRaises(Exception) as ctx:
            self.call(Stream([], fail=err))
        self.assertNotIsInstance(ctx.exception, provider_routing.Unavailable)

    def test_tools_are_offered_under_the_agents_own_name(self):
        seen = {}
        def create(**kw):
            seen.update(kw)
            return Stream([NS(type="response.completed", response=NS(output=[]))])
        self.agent._responses_call(NS(responses=NS(create=create)), model="m", instructions="", input_items=[],
                                   tools=[{"type": "function", "name": "read_file"}], plan_mode=True)
        space = next(t for t in seen["tools"] if t.get("type") == "namespace")
        self.assertEqual(space["name"], "tacitus")
        self.assertIn("Tacitus", space["description"])


if __name__ == "__main__":
    unittest.main()
