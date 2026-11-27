"""The agent is assembled from fourteen parts (engine.AgentRuntime and agent_app.AgentApp list them), and each part
reads attributes that another part or the role defines. A type checker reads each part alone and cannot see that
contract, so it is checked here on the agents as built (2026-11-27): every `self.<name>` a part reads must exist on
every agent that inherits the part. A rename or removal that leaves a reader behind fails here, not on the rare path
in production that reads it."""
import ast
import inspect
import tempfile
import textwrap
import unittest

from agent_harness import AGENTS, make_agent

# Read only behind getattr(self, name, None) by the method that creates it on first use.
CREATED_ON_FIRST_USE = {"_outbox"}


def reads(cls):
    """{name: line} of every self.<name> the class's own source reads."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(cls)))
    found = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self"
                and not isinstance(node.ctx, ast.Store)):
            found.setdefault(node.attr, node.lineno)
    return found


class PartsContract(unittest.TestCase):
    def test_every_attribute_a_part_reads_exists_on_every_agent_built_with_it(self):
        for name in AGENTS:
            with self.subTest(agent=name), tempfile.TemporaryDirectory() as tmp:
                agent = make_agent(name, tmp)
                parts = [c for c in type(agent).__mro__ if c.__module__.startswith("tabularium")]
                self.assertGreater(len(parts), 10)
                missing = [f"{c.__module__}.{c.__name__} line {line}: self.{attr}"
                           for c in parts for attr, line in reads(c).items()
                           if attr not in CREATED_ON_FIRST_USE and not hasattr(agent, attr)]
                self.assertEqual(missing, [])

    def test_every_tool_an_agent_is_offered_has_a_branch_that_handles_it(self):
        """A tool is offered by name in tool_schemas and handled by name in the dispatchers; a name in the first and
        not the second reached the model and came back 'Unknown tool' only when called."""
        dispatchers = ("dispatch_extra", "_execute_tool_impl", "_execute_tool_unguarded", "execute_tool")
        for name in AGENTS:
            with self.subTest(agent=name), tempfile.TemporaryDirectory() as tmp:
                agent = make_agent(name, tmp)
                handled = set()
                for c in type(agent).__mro__:
                    for fn in dispatchers:
                        if c.__module__.startswith("tabularium") and fn in c.__dict__:
                            for node in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(c.__dict__[fn])))):
                                if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == "name":
                                    handled |= {k.value for comp in node.comparators for k in ast.walk(comp)
                                                if isinstance(k, ast.Constant) and isinstance(k.value, str)}
                for provider in ("anthropic", "openai"):
                    offered = {t.get("name") or t.get("function", {}).get("name") for t in agent.available_tools(provider)}
                    self.assertGreater(len(offered), 10)
                    self.assertEqual(sorted(offered - handled), [], provider)


if __name__ == "__main__":
    unittest.main()
