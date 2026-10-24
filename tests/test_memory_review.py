"""Weekly memory review (2026-10-11): candidates are reported, nothing is changed."""
import json, sys, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent_core import memory_review as mr

MEMORY = """# Memory

## Alex — Who He Is
- Alex prefers short replies and plain prose without decoration.
- Alex prefers short replies in plain prose, without decoration.
- Alex's name is written Alex, never translated.

## Recent Activity
- Alex prefers short replies and plain prose without decoration.
- Alex prefers short replies and plain prose without decoration.

<!-- MEMORY-DETAIL-BELOW -->

## Ongoing Situations
### Alpha Index Engine — ACTIVE (started 2026-01-10)
- last touched 2026-02-01, nothing since.
### Project Two — ACTIVE
- updated 2026-10-09 with the new file names.

## Sourced facts

### Fact aaaaaaaaaaaaaaaa
```json
{"content": "Alex is in Tokyo next week", "valid_to": "2026-09-01"}
```

### Fact bbbbbbbbbbbbbbbb
```json
{"content": "Alex likes coffee", "valid_to": null}
```

## Superseded facts (historical, not current)

### Fact cccccccccccccccc
```json
{"content": "old", "valid_to": "2025-01-01"}
```
"""


class Review(unittest.TestCase):
    def test_candidates_found_historical_sections_ignored(self):
        r = mr.review(MEMORY, today="2026-10-11")
        self.assertEqual(len(r["duplicates"]), 1)                       # the pair in Who He Is; Recent Activity skipped
        self.assertEqual(r["duplicates"][0]["a_section"], "Alex — Who He Is")
        self.assertEqual([f["id"] for f in r["expired_facts"]], ["aaaaaaaaaaaaaaaa"])   # c is already historical
        self.assertEqual([s["heading"][:12] for s in r["stale_active"]], ["Alpha Index "])
        self.assertEqual(r["candidates"], 3)
        self.assertLess(r["core_lines"], r["total_lines"])

    def test_run_writes_report_and_summary_without_touching_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / "memory.md").write_text(MEMORY, encoding="utf-8")
            before = (root / "memory.md").read_bytes()
            result = mr.run(root, today="2026-10-11")
            self.assertEqual((root / "memory.md").read_bytes(), before)
            report = (root / "task_state" / "memory_review.md").read_text(encoding="utf-8")
            self.assertIn("3 candidate clean-up(s)", report)
            self.assertIn("Fact aaaaaaaaaaaaaaaa", report)
            self.assertIn("change nothing until he agrees", report)
            saved = json.loads((root / "task_state" / "memory_review.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["candidates"], result["candidates"])
            self.assertIsNone(saved["shown_at"])

    def test_clean_memory_has_no_candidates(self):
        r = mr.review("# Memory\n\n## Alex\n- one thing\n- a completely different thing about work\n", today="2026-10-11")
        self.assertEqual(r["candidates"], 0)
        self.assertIn("Nothing to propose", mr.report_markdown(r))


if __name__ == "__main__":
    unittest.main()
