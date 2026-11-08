"""The desktop tool (tabularium.tools.desktop): taken per task on Alex's yes, released by his next message, the clock, a
credential window or his hand on the mouse; coordinates mapped from the screenshot the model saw."""
import base64
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from tabularium.tools import desktop

from tabularium.records import task_decisions as td
from tabularium.core.outcome import Refused
from agent_harness import make_agent
from tabularium.channels.owner_telegram import OWNER_CHAT_ID


class FakeDriver:
    def __init__(self):
        self.calls = []

    def click(self, x, y, button="left", clicks=1):
        self.calls.append(("click", x, y, button, clicks))

    def write(self, text):
        self.calls.append(("write", text))

    def hotkey(self, *keys):
        self.calls.append(("hotkey",) + keys)

    def scroll(self, ticks, x, y):
        self.calls.append(("scroll", ticks, x, y))


class DesktopTool(unittest.TestCase):
    RECT = (-1920, 0, 0, 1080)                                   # a left-hand monitor: negative screen coordinates

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.agent = make_agent("cato", self.root)
        self.agent.send_message = lambda chat, text: None
        self.driver = FakeDriver()
        for name, value in (("monitors", lambda: [(0, 0, 1920, 1080), self.RECT]),
                            ("grab", lambda rect: Image.new("RGB", (rect[2] - rect[0], rect[3] - rect[1]), "white")),
                            ("foreground_title", lambda: "Settings"),
                            ("driver", lambda: self.driver)):
            p = patch.object(desktop, name, value)
            p.start()
            self.addCleanup(p.stop)

    def run_tool(self, **inputs):
        return self.agent.execute_tool("desktop", inputs, OWNER_CHAT_ID)

    def granted(self):
        """Alex's yes: the runtime runs the parked take with _approving set."""
        self.agent._approving = ("desktop", {"action": "take", "purpose": "open the printer dialog"})
        try:
            return self.run_tool(action="take", purpose="open the printer dialog")
        finally:
            self.agent._approving = None

    def test_take_is_parked_for_his_yes_and_nothing_moves_before_it(self):
        result = self.run_tool(action="take", purpose="open the printer dialog")
        self.assertIsInstance(result, Refused)
        self.assertIn("parked", result)
        actions = td.pending(self.agent._background, OWNER_CHAT_ID, kind="action")
        self.assertEqual(len(actions), 1)
        self.assertEqual(json.loads(actions[0]["action"])["inputs"], {"action": "take", "purpose": "open the printer dialog"})
        self.assertIsInstance(self.run_tool(action="click", x=1, y=1), Refused)
        self.assertEqual(self.driver.calls, [])

    def test_his_yes_grants_and_the_screenshot_frame_maps_clicks_to_the_screen(self):
        self.assertIn("Desktop taken", self.granted())
        shot = self.run_tool(action="screenshot", screen=1)
        self.assertEqual(shot[0]["type"], "image")
        image = Image.open(io.BytesIO(base64.b64decode(shot[0]["source"]["data"])))
        self.assertEqual(image.size, (1568, 882))                 # 1920x1080 scaled to the long edge
        self.assertIn("Screen 1 of 2", shot[1]["text"])
        self.assertNotIsInstance(self.run_tool(action="click", x=784, y=441, double=True), Refused)
        kind, x, y, button, clicks = self.driver.calls[-1]
        self.assertEqual((kind, button, clicks), ("click", "left", 2))
        self.assertEqual((x, y), (-960, 540))                     # the centre of the left monitor
        self.run_tool(action="type", text="hello")
        self.run_tool(action="key", keys="Ctrl+S")
        self.run_tool(action="scroll", x=10, y=10, direction="up", amount=2)
        self.assertEqual([c[0] for c in self.driver.calls[-3:]], ["write", "hotkey", "scroll"])
        self.assertEqual(self.driver.calls[-2], ("hotkey", "ctrl", "s"))
        self.assertEqual(self.run_tool(action="release"), "Desktop released.")
        self.assertIsNone(desktop.grant(self.agent.AGENT_DIR))

    def test_a_click_before_any_screenshot_is_refused(self):
        self.granted()
        result = self.run_tool(action="click", x=1, y=1)
        self.assertIsInstance(result, Refused)
        self.assertIn("screenshot first", result)

    def test_his_next_message_releases_the_desktop(self):
        self.granted()
        self.run_tool(action="screenshot")
        with self.agent._inbox.db() as db:
            db.execute("INSERT INTO jobs(chat,message,handler,payload,status,created) VALUES (?,?,?,?,?,?)",
                       (OWNER_CHAT_ID, 99, "handle_message", "{}", "pending", time.time()))
        result = self.run_tool(action="click", x=1, y=1)
        self.assertIsInstance(result, Refused)
        self.assertIn("sent a message", result)
        self.assertIsNone(desktop.grant(self.agent.AGENT_DIR))
        self.assertEqual(self.driver.calls, [])

    def test_the_grant_expires(self):
        self.granted()
        g = desktop.grant(self.agent.AGENT_DIR)
        g["since"] -= desktop.GRANT_SECONDS + 1
        desktop._save(self.agent.AGENT_DIR, g)
        self.assertIn("expired", self.run_tool(action="screenshot"))
        self.assertIsNone(desktop.grant(self.agent.AGENT_DIR))

    def test_a_credential_window_refuses_input_in_code(self):
        self.granted()
        self.run_tool(action="screenshot")
        with patch.object(desktop, "foreground_title", lambda: "Sign in to your Microsoft account"):
            for inputs in ({"action": "click", "x": 1, "y": 1}, {"action": "type", "text": "hunter2"}, {"action": "key", "keys": "enter"}):
                result = self.run_tool(**inputs)
                self.assertIsInstance(result, Refused)
                self.assertIn("credential or payment", result)
        self.assertEqual(self.driver.calls, [])

    def test_his_hand_on_the_mouse_releases_the_grant(self):
        self.granted()
        self.run_tool(action="screenshot")

        class FailSafeException(Exception):
            pass

        def click(*a, **k):
            raise FailSafeException()
        self.driver.click = click
        result = self.run_tool(action="click", x=1, y=1)
        self.assertIsInstance(result, Refused)
        self.assertIn("took the desktop back", result)
        self.assertIsNone(desktop.grant(self.agent.AGENT_DIR))

    def test_one_driver_at_a_time_and_never_from_a_background_task(self):
        tree = self.root / "agents"
        for name, since in (("vitruvius", time.time()), ("callimachus", time.time() - desktop.GRANT_SECONDS - 1)):
            (tree / name / "task_state").mkdir(parents=True)
            (tree / name / "task_state" / "desktop_grant.json").write_text(json.dumps({"chat": OWNER_CHAT_ID, "job": 0, "since": since}))
        self.assertEqual(desktop.holder(tree, "cato"), "vitruvius")      # a live grant counts; an expired one does not
        self.assertEqual(desktop.holder(tree, "vitruvius"), "")
        with patch.object(desktop, "holder", lambda root, exclude: "vitruvius"):
            result = self.run_tool(action="take", purpose="x")
        self.assertIsInstance(result, Refused)
        self.assertIn("vitruvius holds the desktop", result)
        self.agent.BACKGROUND_TASK_ID = "1"
        result = self.run_tool(action="take", purpose="x")
        self.assertIsInstance(result, Refused)
        self.assertIn("foreground only", result)

    def test_every_agent_offers_the_tool(self):
        for name in ("cato", "tacitus", "callimachus", "vitruvius"):
            with tempfile.TemporaryDirectory() as tmp:
                agent = make_agent(name, Path(tmp))
                self.assertIn("desktop", [t["name"] for t in agent.get_tools("anthropic")], name)


if __name__ == "__main__":
    unittest.main()
