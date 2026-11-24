"""Claude subscription route: replayed history must show tool names as the SDK exposes them (2026-10-09)."""
import sys, unittest
from pathlib import Path
from tabularium.models import subscription_runtime as sr
class SdkToolNames(unittest.TestCase):
    def test_bare_names_get_prefix_and_rest_untouched(self):
        history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"},
                                              {"type": "tool_use", "id": "1", "name": "read_memory", "input": {"q": "x"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "1", "content": "..."}]},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "2", "name": "mcp__agent__read_file", "input": {}}]},
        ]
        out = sr.sdk_tool_names(history)
        self.assertEqual(out[1]["content"][1]["name"], "mcp__agent__read_memory")
        self.assertEqual(out[3]["content"][0]["name"], "mcp__agent__read_file")      # not doubled
        self.assertEqual(out[0], history[0])
        self.assertEqual(history[1]["content"][1]["name"], "read_memory")             # input not mutated
        self.assertEqual(out[1]["content"][1]["input"], {"q": "x"})


if __name__ == "__main__":
    unittest.main()
