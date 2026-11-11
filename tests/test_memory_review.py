"""Weekly memory review (2026-10-11): candidates are reported, nothing is changed."""
import json, sys, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
from tabularium.memory import memory_review as mr
MEMORY = """# Memory

## Alex — Who He Is
- Alex prefers short replies and plain prose without decoration.
- Alex prefers short replies in plain prose, without decoration.
- Alex likes green tea.

## Recent Activity
- Alex prefers short replies and plain prose without decoration.
- Alex prefers short replies and plain prose without decoration.

<!-- MEMORY-DETAIL-BELOW -->

## Ongoing Situations
### Garden Plan Review — ACTIVE (started 2026-01-10)
- last touched 2026-02-01, nothing since.
### Book Club Notes — ACTIVE
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
        self.assertEqual([s["heading"][:12] for s in r["stale_active"]], ["Garden Plan "])
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

    def test_a_later_message_that_changed_a_sourced_fact_is_a_candidate(self):
        fact = ('## Sourced facts\n\n### Fact dddddddddddddddd\n```json\n' + json.dumps({
            "content": "Email goes out through the new Outlook app", "kind": "user_decision",
            "evidence": {"source": "message:10", "said_at": "2026-11-01T10:00:00+00:00", "quote": "use the new outlook app"}}) + '\n```\n')
        text = MEMORY.replace("## Sourced facts\n", fact, 1)

        class Recall:
            def search(self, chat, query="", speaker="", date_from="", limit=5, **_):
                assert speaker == "user" and date_from == "2026-11-01"
                return {"matches": [{"id": 12, "said_at": "2026-11-03T09:00:00", "excerpt": "改做thunderbird"},
                                    {"id": 9, "said_at": "2026-10-30T09:00:00", "excerpt": "outlook again"}]}

        asked = []

        def examiner(prompt, schema):
            asked.append(prompt)
            return json.dumps({"changed": True, "message_id": 12, "quote": "改做thunderbird", "note": "method replaced"})

        r = mr.review(text, "2026-11-10", chat=1, recall=Recall(), examiner=examiner)
        self.assertEqual([(f["id"], f["later_id"], f["quote"]) for f in r["changed_facts"]], [("dddddddddddddddd", 12, "改做thunderbird")])
        self.assertIn("message 12", asked[0]); self.assertNotIn("message 9", asked[0])     # only later messages are shown
        self.assertEqual(r["candidates"], 1 + len(r["duplicates"]) + len(r["expired_facts"]) + len(r["stale_active"]))
        self.assertIn("supersedes=<fact id>", mr.report_markdown(r))
        # A verdict naming a message that was not shown counts nothing; an examiner failure is reported, not silent.
        r = mr.review(text, "2026-11-10", chat=1, recall=Recall(), examiner=lambda p, s: json.dumps({"changed": True, "message_id": 9, "quote": "", "note": ""}))
        self.assertEqual(r["changed_facts"], [])

        def broken(prompt, schema):
            raise RuntimeError("model unavailable")
        r = mr.review(text, "2026-11-10", chat=1, recall=Recall(), examiner=broken)
        self.assertEqual((r["changed_facts"], r["error"]), ([], "RuntimeError: model unavailable"))
        self.assertIn("stopped early", mr.report_markdown(r))
        # Without the record and the examiner the review reads the file alone, as before.
        self.assertEqual(mr.review(text, "2026-11-10")["changed_facts"], [])

    def test_clean_memory_has_no_candidates(self):
        r = mr.review("# Memory\n\n## Alex\n- one thing\n- a completely different thing about work\n", today="2026-10-11")
        self.assertEqual(r["candidates"], 0)
        self.assertIn("Nothing to propose", mr.report_markdown(r))


if __name__ == "__main__":
    unittest.main()
