"""Two-pass context archiving keeps a request under budget without touching recent results or the saved history.
2026-10-07 task 82: 69 web-search results of 5-10 KB each sat under the single 12 KB archive threshold and the context
grew past 160k tokens a turn."""
import sys, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
import tabularium.core.context_window as cw


def history(n, size):
    msgs = [{"role": "user", "content": "seed"}, {"role": "assistant", "content": "ok"}]
    for i in range(n):
        msgs.append({"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "web_search", "input": {"q": str(i)}}]})
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "x" * size}]})
    return msgs


class TwoPassArchive(unittest.TestCase):
    def test_medium_results_are_archived_when_over_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            msgs = history(70, 8000)
            out = cw.prepare_context(msgs, Path(tmp), budget_bytes=160_000)
            self.assertLessEqual(cw.wire_size(out), 160_000)
            tail = [b for m in out[-6:] if isinstance(m.get("content"), list) for b in m["content"] if b.get("type") == "tool_result"]
            self.assertTrue(all(not b["content"].startswith(cw.ARCHIVE_MARKER) for b in tail))
            self.assertEqual(cw.wire_size(msgs), cw.wire_size(history(70, 8000)), "saved history mutated")

    def test_small_history_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            msgs = history(3, 8000)      # every result inside the protected tail: nothing to compact by age either
            self.assertIs(cw.prepare_context(msgs, Path(tmp), budget_bytes=160_000), msgs)


if __name__ == "__main__":
    unittest.main()
