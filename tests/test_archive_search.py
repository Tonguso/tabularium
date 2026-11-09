"""search_archived finds passages in archived tool output and hands back offsets for read_file (2026-10-18)."""
import contextlib, io, tempfile, unittest
from pathlib import Path
from tabularium.memory import archive_search
from tabularium.core.outcome import Failed
from agent_harness import make_agent


class ArchiveSearch(unittest.TestCase):
    def test_terms_split_english_and_chinese(self):
        self.assertEqual(archive_search.terms("Leasing demand, 租赁需求"), ["leasing", "demand", "租赁", "赁需", "需求"])
        self.assertEqual(archive_search.terms("Q4 2025 revenue"), ["q4", "2025", "revenue"])

    def test_best_passage_and_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "context_archive" / "results"; root.mkdir(parents=True)
            text = ("ITEM 1A. RISK FACTORS\n\n" + "Nothing of note here. " * 20 + "\n\n"
                    "A significant decrease in leasing demand for our communications infrastructure would hurt results.\n\n"
                    "Unrelated paragraph about taxes.")
            (root / "a.txt").write_text(text, encoding="utf-8")
            (root / "b.txt").write_text("Another archive about towers and leasing.", encoding="utf-8")
            out = archive_search.search(Path(tmp) / "context_archive", "leasing demand")
            self.assertIn("leasing demand for our communications", out)
            offset = int(out.split("@ ")[1].split("]")[0])
            self.assertEqual(text[offset:offset + 22], "A significant decrease")        # read_file(path, offset) lands on it
            self.assertTrue(out.index("a.txt") < out.index("b.txt"))                    # both words beat one word
            self.assertIn("No passage matches", archive_search.search(Path(tmp) / "context_archive", "helium"))
            self.assertIsInstance(archive_search.search(Path(tmp) / "context_archive", "x", path=r"C:\Windows\win.ini"), Failed)

    def test_every_agent_has_it_after_read_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "coordinator"; root.mkdir()
            with contextlib.redirect_stdout(io.StringIO()):
                a = make_agent("coordinator", root)
            names = [t["name"] for t in a.get_tools("anthropic")]
            self.assertEqual(names[names.index("read_file") + 1], "search_archived")
            self.assertIn("No archived tool output yet", a.execute_tool("search_archived", {"query": "anything"}, 1))


if __name__ == "__main__":
    unittest.main()
