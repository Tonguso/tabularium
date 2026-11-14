"""One click switches an agent's model (2026-11-14): /api/model writes the temporary route the runtime reads every
turn; an empty kind clears it. The sequence editor with its drafts, Save and confirm is gone."""
import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from tabularium.dashboard import server as dashboard
from tabularium.models import provider_routing as routing


class ModelSwitch(unittest.TestCase):
    def test_one_click_sets_and_clears_the_temporary_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / "coordinator").mkdir()
            routing.save(root / "coordinator", {"enabled": True, "routes": [{"kind": "anthropic_subscription", "model": "claude-opus-5-5"}]})
            store = Mock(); store.decisions.return_value = []
            with patch.object(dashboard, "ROOT", root), patch.object(dashboard, "task_store", return_value=store):
                server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                try:
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                    headers = {"Host": f"127.0.0.1:{dashboard.PORT}", "Origin": dashboard.URL,
                               "Content-Type": "application/json", "X-CSRF-Token": dashboard.token()}

                    def post(body):
                        connection.request("POST", "/api/model", json.dumps(body), headers)
                        response = connection.getresponse()
                        return response.status, json.loads(response.read())

                    status, out = post({"agent": "coordinator", "kind": "openai_subscription", "model": "gpt-6.1-sol"})
                    self.assertEqual(status, 200); self.assertIn("gpt-6.1-sol", out["message"])
                    current = routing.override(root / "coordinator")
                    self.assertEqual((current["kind"], current["model"]), ("openai_subscription", "gpt-6.1-sol"))
                    self.assertEqual(routing.effective_routes(root / "coordinator")[0]["model"], "gpt-6.1-sol")
                    status, out = post({"agent": "coordinator", "kind": "openai_subscription", "model": ""})
                    self.assertEqual(status, 400)                                   # refused, the route stays
                    self.assertEqual(routing.override(root / "coordinator")["model"], "gpt-6.1-sol")
                    status, out = post({"agent": "coordinator", "kind": "", "model": ""})
                    self.assertEqual(status, 200); self.assertIn("default sequence", out["message"])
                    self.assertIsNone(routing.override(root / "coordinator"))
                    self.assertFalse((root / "coordinator" / routing.OVERRIDE).exists())
                finally:
                    server.shutdown()


if __name__ == "__main__":
    unittest.main()
