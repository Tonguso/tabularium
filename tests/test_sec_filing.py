"""sec_filing: EDGAR as a primary source for Cato and Cal, offline-tested with a fake edgar module (2026-10-17)."""
import contextlib, io, os, sys, tempfile, types, unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
import pandas as pd
from agent_core import sec_edgar
from agent_core.outcome import Failed
from agent_harness import make_agent


class FakeFiling:
    def __init__(self, form, date, acc):
        self.form, self.filing_date, self.accession_no = form, date, acc
        self.url = f"https://www.sec.gov/Archives/{acc}-index.html"
    def obj(self):
        if self.form == "4":
            return NS(to_dataframe=lambda: pd.DataFrame([{"Date": "2026-10-01", "Insider": "P Blanchett", "Position": "CAO",
                                                          "Transaction Type": "Award", "Code": "A", "Shares": 16299, "Price": 0,
                                                          "Value": None, "Remaining Shares": 16299}]))
        return {"Item 1A": "Risks about leasing demand.", "Item 7": "MD&A text.", "Item 1": "", "Item 2": "Quarter MD&A."}


class FakeFilings(list):
    def head(self, n): return FakeFilings(self[:n])


class FakeCompany:
    name, cik = "AMERICAN TOWER CORP /MA/", 1053507
    def __init__(self, ticker):
        if ticker != "AMT": raise LookupError(f"Company not found: {ticker!r}")
    def get_filings(self, form):
        forms = form if isinstance(form, list) else [form]
        rows = [FakeFiling("10-K", "2026-02-24", "0001-26-35"), FakeFiling("8-K", "2026-10-02", "0001-26-99"), FakeFiling("4", "2026-10-05", "0001-26-77")]
        return FakeFilings([r for r in rows if r.form in forms])
    def latest(self, form): return next((r for r in self.get_filings(form)), None)
    def income_statement(self, periods=4, period="annual"):
        frame = pd.DataFrame({"label": ["Total Revenue", "Operating Income"], "depth": [0, 0], "is_abstract": [False, False],
                              "is_total": [True, False], "section": ["x", "x"], "confidence": [1, 1],
                              "FY 2025": ["$10.6B", "$4.8B"], "FY 2024": ["$10.1B", "$4.5B"]})
        return NS(to_dataframe=lambda: frame)
    balance_sheet = cash_flow_statement = income_statement


class SecFiling(unittest.TestCase):
    def setUp(self):
        fake = types.ModuleType("edgar"); fake.Company = FakeCompany; fake.set_identity = lambda s: None
        self.mods = patch.dict(sys.modules, {"edgar": fake}); self.mods.start()
        self.env = patch.dict(os.environ, {"EDGAR_IDENTITY": "Test Person test@example.com"}); self.env.start()

    def tearDown(self):
        self.mods.stop(); self.env.stop()

    def test_filings_section_financials_insiders_cite_the_filing(self):
        out = sec_edgar.sec_filing("filings", "amt")
        self.assertIn("- 10-K  2026-02-24  0001-26-35  https://www.sec.gov/Archives/0001-26-35-index.html", out)
        self.assertNotIn("Form 4", out)                                       # not asked for
        out = sec_edgar.sec_filing("section", "AMT")
        self.assertTrue(out.startswith("Source: SEC EDGAR, AMERICAN TOWER CORP /MA/ (CIK 1053507), 10-K filed 2026-02-24, https://"), out)
        self.assertIn("Item 1A (27 characters)\n\nRisks about leasing demand.", out)
        self.assertIn("MD&A text.", sec_edgar.sec_filing("section", "AMT", item="mdna"))
        self.assertIsInstance(sec_edgar.sec_filing("section", "AMT", item="Item 1"), Failed)     # empty item
        out = sec_edgar.sec_filing("financials", "AMT", statement="income", periods=2)
        self.assertIn("Total Revenue", out); self.assertIn("$10.6B", out); self.assertNotIn("is_abstract", out)
        out = sec_edgar.sec_filing("insiders", "AMT")
        self.assertIn("P Blanchett", out); self.assertIn("0001-26-77-index.html", out)

    def test_failures_are_typed(self):
        self.assertIsInstance(sec_edgar.sec_filing("filings", "ZZZZ"), Failed)
        self.assertIsInstance(sec_edgar.sec_filing("dance", "AMT"), Failed)
        with patch.dict(os.environ, {"EDGAR_IDENTITY": ""}):
            out = sec_edgar.sec_filing("filings", "AMT")
            self.assertIsInstance(out, Failed); self.assertIn("EDGAR_IDENTITY", out)

    def test_only_the_research_agents_have_it(self):
        have = {}
        for agent in ("cato", "callimachus", "tacitus", "vitruvius"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / agent; root.mkdir()
                with contextlib.redirect_stdout(io.StringIO()):
                    a = make_agent(agent, root)
                have[agent] = "sec_filing" in {t["name"] for t in a.get_tools("anthropic")}
        self.assertEqual(have, {"cato": True, "callimachus": True, "tacitus": False, "vitruvius": False})


if __name__ == "__main__":
    unittest.main()
