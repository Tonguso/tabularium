"""Compaction preserves the prefix below the limit and archives stale results at a context boundary."""
import json, sys, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
from tabularium.core import context_window as cw
def turn(i, tool, size, protected=False):
    """One assistant tool_use plus its user tool_result, `size` characters long."""
    return [{"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": tool, "input": {"q": i}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": f"result {i} " + "x" * size}]}]


class AgeCompaction(unittest.TestCase):
    def test_old_big_results_become_previews_recent_and_protected_stay(self):
        with tempfile.TemporaryDirectory() as tmp:
            msgs = [{"role": "user", "content": "core memory"}, {"role": "assistant", "content": "ready"}]
            msgs += turn(1, "read_file", 5000)                 # old and big: compacted
            msgs += turn(2, "skill", 5000)                     # old but protected: kept
            msgs += turn(3, "web_search", 500)                 # old but small: kept
            for i in range(4, 8):
                msgs += turn(i, "read_file", 5000)             # turn 4 is stale too; turns 5-7 are the protected tail (6 messages)
            original = json.dumps(msgs)
            self.assertIs(cw.prepare_context(msgs, Path(tmp)), msgs)
            out = cw.prepare_context(msgs, Path(tmp), budget_bytes=25_000)
            self.assertIsNot(out, msgs)
            self.assertEqual(json.dumps(msgs), original)      # canonical history untouched
            def body(idx): return out[idx]["content"][0]["content"]
            self.assertTrue(body(3).startswith(cw.ARCHIVE_MARKER), body(3)[:80])
            self.assertFalse(body(5).startswith(cw.ARCHIVE_MARKER))             # skill output protected
            self.assertEqual(body(7), msgs[7]["content"][0]["content"])         # small result untouched
            tail_bodies = [out[i]["content"][0]["content"] for i in range(len(out) - 6, len(out)) if out[i]["role"] == "user"]
            self.assertTrue(all(not b.startswith(cw.ARCHIVE_MARKER) for b in tail_bodies))
            self.assertLess(cw.wire_size(out), cw.wire_size(msgs))
            archived = list(Path(tmp).glob("*.txt"))
            self.assertEqual(len(archived), 2)                                   # turns 1 and 4 archived, retrievable
            self.assertTrue(body(9).startswith(cw.ARCHIVE_MARKER))
            self.assertTrue(any("result 1" in p.read_text(encoding="utf-8") for p in archived))

    def test_short_history_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            msgs = [{"role": "user", "content": "m"}, {"role": "assistant", "content": "r"}] + turn(1, "read_file", 9000)
            self.assertIs(cw.prepare_context(msgs, Path(tmp)), msgs)


if __name__ == "__main__":
    unittest.main()
