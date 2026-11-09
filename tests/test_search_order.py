"""Offline: one search-engine order for every agent, set on the dashboard (search_order.json): default
AnySearch -> Tavily -> model routes; the shared entry point follows it; each agent prints it at startup;
the SEARCH_PROVIDER switch is gone."""
import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from tabularium.tools import search_routing as sr
AGENTS = ("coordinator", "private", "librarian", "engineer")


class SearchOrderFile(unittest.TestCase):
    def setUp(self):
        self._file = sr.ORDER_FILE
        sr.ORDER_FILE = Path(tempfile.mkdtemp()) / "search_order.json"

    def tearDown(self):
        sr.ORDER_FILE = self._file

    def test_default_and_round_trip(self):
        self.assertEqual(sr.read_order(), ["anysearch", "tavily", "model"])
        self.assertEqual(sr.save_order(["tavily", "model"]), ["tavily", "model"])
        self.assertEqual(json.loads(sr.ORDER_FILE.read_text())["order"], ["tavily", "model"])
        self.assertEqual(sr.order_label(tavily_key=False), "Tavily (no key) -> model routes")

    def test_validation(self):
        for bad in ([], ["google"], ["tavily", "tavily"], "anysearch", None):
            with self.assertRaises(ValueError):
                sr.save_order(bad)
        self.assertEqual(sr.read_order(), ["anysearch", "tavily", "model"])   # nothing written by a refused save


class SearchOrderWiring(unittest.TestCase):
    def test_shared_entry_point_follows_the_file(self):
        src = (ROOT / "src" / "tabularium" / "core" / "engine.py").read_text(encoding="utf-8")
        fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == "tool_web_search")
        body = ast.unparse(fn)
        self.assertIn("read_order()", body)
        for engine in ("anysearch", "tavily", "model"):
            self.assertIn(f"'{engine}'", body)
        self.assertNotIn("SEARCH_PROVIDER", src)

    def test_every_agent_prints_the_order_and_has_no_switch(self):
        app = (ROOT / "src" / "tabularium" / "core" / "agent_app.py").read_text(encoding="utf-8")      # one message path since 2026-10-11
        self.assertIn('print("Search order: " + _search_order.order_label(', app)
        self.assertNotIn("SEARCH_PROVIDER", app)
        for agent in (ROOT / "src" / "tabularium" / "agents").glob("*.py"):
            self.assertNotIn("SEARCH_PROVIDER", agent.read_text(encoding="utf-8"), agent.name)

    def test_dashboard_exposes_and_saves_the_order(self):
        src = (ROOT / "src" / "tabularium" / "dashboard" / "server.py").read_text(encoding="utf-8")
        self.assertIn("data['search_order']=search_routing.read_order()", src)
        self.assertIn("'/api/search_order'", src)
        self.assertIn('id="search-order"', (ROOT / "src" / "tabularium" / "dashboard" / "static" / "index.html").read_text(encoding="utf-8"))
        self.assertIn("function searchOrderPanel", (ROOT / "src" / "tabularium" / "dashboard" / "static" / "app.js").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
