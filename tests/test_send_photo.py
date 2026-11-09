"""send_photo carries a local render to Alex, marked up: a question about a shape goes out as the picture with the
candidates drawn on it (2026-11-08), not as a numbered list of geometry."""
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from PIL import Image

from agent_harness import make_agent
from tabularium.channels.owner_telegram import OWNER_CHAT_ID


class SendPhoto(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.agent = make_agent("coordinator", self.root)
        self.agent.bot = Mock()
        self.png = self.root / "board.png"
        Image.new("RGB", (400, 200), "white").save(self.png)

    def sent_image(self):
        self.agent.bot.send_photo.assert_called_once()
        args, kwargs = self.agent.bot.send_photo.call_args
        return Image.open(io.BytesIO(args[1].getvalue())).convert("RGB"), kwargs.get("caption")

    def test_a_local_file_is_sent_with_marks_drawn_and_the_crop_applied(self):
        out = self.agent.execute_tool("send_photo", {"path": str(self.png), "caption": "This piece?",
                                                     "marks": [{"x": 20, "y": 20, "w": 60, "h": 40, "label": "A"}],
                                                     "crop": [0, 0, 200, 100]}, OWNER_CHAT_ID)
        self.assertIn("Photo sent", out)
        image, caption = self.sent_image()
        self.assertEqual(image.size, (200, 100))                       # the crop, in the original file's pixels
        self.assertEqual(caption, "This piece?")
        r, g, b = image.getpixel((20, 40))                              # on the box's left edge
        self.assertTrue(r > 200 and g < 80 and b < 80, (r, g, b))
        self.assertEqual(image.getpixel((150, 80)), (255, 255, 255))    # the rest untouched

    def test_an_unmarked_local_file_is_sent_as_it_is_and_nothing_is_sent_without_a_source(self):
        self.assertIn("Give path", self.agent.execute_tool("send_photo", {"caption": "x"}, OWNER_CHAT_ID))
        self.agent.bot.send_photo.assert_not_called()
        self.assertIn("No file at", self.agent.execute_tool("send_photo", {"path": str(self.root / "missing.png")}, OWNER_CHAT_ID))
        self.agent.execute_tool("send_photo", {"path": str(self.png)}, OWNER_CHAT_ID)
        image, _ = self.sent_image()
        self.assertEqual(image.size, (400, 200))


if __name__ == "__main__":
    unittest.main()
