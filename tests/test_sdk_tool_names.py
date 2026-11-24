"""Claude subscription route: replayed history must show tool names as the SDK exposes them (2026-10-09)."""
import json, sys, unittest
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


class TranscriptBlocks(unittest.TestCase):
    """The conversation is sent as one block per exchange, so the part that did not change is read from the cache
    (2026-11-24: sent as one text, every turn wrote the whole conversation to the cache again)."""
    MARK = {"type": "ephemeral", "ttl": "1h"}
    PINNED = [{"role": "user", "content": "memory"}, {"role": "assistant", "content": "noted"}]

    def exchange(self, n, attached=""):
        return [{"role": "user", "content": attached + f"question {n}"},
                {"role": "assistant", "content": [{"type": "tool_use", "id": str(n), "name": "mcp__agent__read_file", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": str(n), "content": "..."}]},
                {"role": "assistant", "content": f"answer {n}"}]

    def test_one_block_per_exchange_and_the_marker_before_the_newest(self):
        replay = self.PINNED + self.exchange(1) + self.exchange(2) + [{"role": "user", "content": "core memory\n\nquestion 3\n\nlive state"}]
        blocks = sr.transcript_blocks(replay, self.MARK)
        self.assertEqual(len(blocks), 4)                                   # pinned, two exchanges, the newest
        self.assertEqual([i for i, b in enumerate(blocks) if "cache_control" in b], [2])
        self.assertEqual(blocks[2]["cache_control"], self.MARK)
        self.assertEqual(sum((json.loads(b["text"]) for b in blocks), []), replay)   # nothing lost, nothing reordered

    def test_what_was_marked_is_the_same_text_on_the_next_turn(self):
        first = self.PINNED + self.exchange(1) + [{"role": "user", "content": "core memory\n\nquestion 2\n\nlive state"}]
        after = self.PINNED + self.exchange(1) + self.exchange(2) + [{"role": "user", "content": "new core\n\nquestion 3\n\nnew state"}]
        before, later = sr.transcript_blocks(first, self.MARK), sr.transcript_blocks(after, self.MARK)
        marked = next(i for i, b in enumerate(before) if "cache_control" in b)
        self.assertEqual([b["text"] for b in before[:marked + 1]], [b["text"] for b in later[:marked + 1]])
        self.assertIn("cache_control", later[marked + 1])                  # the marker moved on by one exchange

    def test_a_single_exchange_has_nothing_to_reuse(self):
        blocks = sr.transcript_blocks([{"role": "user", "content": "hello"}], self.MARK)
        self.assertEqual(len(blocks), 1)
        self.assertNotIn("cache_control", blocks[0])
        self.assertEqual(sr.transcript_blocks([], self.MARK), [])


if __name__ == "__main__":
    unittest.main()
