"""Offline: read_file defaults to an excerpt, and textdiff.py compares two files without a model.
2026-10-07: five subtitle comparisons cost 115k-567k tokens each because both whole files were read into context."""
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ReadFileDefault(unittest.TestCase):
    def test_default_limit_is_an_excerpt_everywhere(self):
        for rel in ("engine.py", "cato/telegram.py"):
            src = (ROOT / rel).read_text(encoding="utf-8")
            for m in re.finditer(r"def tool_read_file\(path: str, offset: int = 0, limit: int = (\d+)\)", src):
                self.assertEqual(m.group(1), "8000", rel)
            self.assertIn("8,000 characters by default", src, rel) if '"name": "read_file"' in src else None


class TextDiff(unittest.TestCase):
    def test_compares_srt_files_and_reports_script_variant(self):
        tmp = Path(tempfile.mkdtemp())
        a = tmp / "a.srt"; b = tmp / "b.srt"
        cue = "{n}\n00:00:0{n},000 --> 00:00:0{n},900\n{t}\n\n"
        a.write_text("".join(cue.format(n=i, t="这是简体字幕") for i in range(1, 5)), encoding="utf-8")
        b.write_text("".join(cue.format(n=i, t="這是繁體字幕" if i == 2 else "这是简体字幕") for i in range(1, 5)), encoding="utf-8-sig")
        r = subprocess.run([sys.executable, str(ROOT / "cato" / "scripts" / "textdiff.py"), str(a), str(b), "--srt"],
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("cues 4", r.stdout)
        self.assertIn("+BOM", r.stdout)
        self.assertIn("similarity", r.stdout)
        self.assertIn("+這是繁體字幕", r.stdout)   # the changed cue appears in the diff
        self.assertIn("diff: 2 changed lines", r.stdout)


if __name__ == "__main__":
    unittest.main()
