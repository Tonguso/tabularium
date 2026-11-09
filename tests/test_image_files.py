"""An image sent as a file is an image (2026-10-13).

Telegram sends a photo compressed, or the same picture as a "document" when the sender chooses "send as file".
Cato answered a 3.2 MB screenshot sent that way with "File type '.jpg' not supported". The document handler now
routes by what the file is: image files take the photo path, downscaled like any image the agent views."""
import base64, io, sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock
ROOT = Path(__file__).resolve().parents[1]
from agent_harness import load_agent


def png(width, height):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


class ImageFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "coordinator"; root.mkdir()
        self.rt = load_agent("coordinator", root)["runtime"]
        self.rt.BACKGROUND_TASK_ID = ""
        self.seen = []
        self.rt.run_turn = lambda messages, chat: self.seen.append(messages[-1]) or "OK"
        self.sent = []
        self.rt.send_message = lambda chat, text: self.sent.append(text)
        self.rt.bot = NS(get_file=lambda fid: NS(file_path="p"), download_file=lambda p: self.payload, send_chat_action=Mock())

    def tearDown(self):
        self.tmp.cleanup()

    def document(self, name, mime, caption="这个script，tidy up一下"):
        return NS(chat=NS(id=1), document=NS(file_id="f", file_name=name, mime_type=mime, file_size=len(self.payload)),
                  caption=caption, date=1791901686, reply_to_message=None, text=None)

    def test_jpg_sent_as_a_file_reaches_the_model_as_an_image_with_its_caption(self):
        self.payload = png(400, 300)
        self.rt.handle_document(self.document("Screenshot_2026-10-13.jpg", "image/jpeg"))
        self.assertEqual(len(self.seen), 1)
        blocks = self.seen[0]["content"]
        self.assertEqual(blocks[0]["type"], "image")
        self.assertEqual(blocks[0]["source"]["media_type"], "image/png")
        text = blocks[-1]["text"]
        self.assertIn("image file: Screenshot_2026-10-13.jpg", text)
        self.assertIn("tidy up", text)
        self.assertEqual(self.sent, ["OK"])                           # the model's reply, no refusal

    def test_a_large_image_file_is_downscaled_like_a_viewed_image(self):
        self.payload = png(4000, 3000)
        self.rt.handle_document(self.document("big.png", "image/png"))
        src = self.seen[0]["content"][0]["source"]
        from PIL import Image
        img = Image.open(io.BytesIO(base64.b64decode(src["data"])))
        self.assertEqual(src["media_type"], "image/jpeg")
        self.assertLessEqual(max(img.size), self.rt.VIEW_IMAGE_MAX_EDGE)

    def test_other_file_types_are_still_refused_politely(self):
        self.payload = b"not an image"
        self.rt.handle_document(self.document("notes.foo", "application/octet-stream", caption=""))
        self.assertEqual(self.seen, [])
        self.assertIn("not supported", self.sent[0])

    def test_a_file_over_telegrams_20_mb_cap_is_named_to_owner_not_an_internal_error(self):
        """2026-10-26: a 20.4 MB screenshot sent as a file failed twice as "Task #2163 stopped with an error"."""
        from tabularium.core.shared_handlers import FileTooBig
        self.payload = b""
        doc = self.document("Screenshot_2026_10_26.jpg", "image/jpeg")
        doc.document.file_size = 21_400_000
        self.rt.bot.get_file = lambda fid: (_ for _ in ()).throw(AssertionError("must not download"))
        with self.assertRaises(FileTooBig) as caught:
            self.rt.handle_document(doc)
        self.assertIn("Screenshot_2026_10_26.jpg is 20.4 MB", str(caught.exception))
        self.assertIn("at most 20 MB", str(caught.exception))
        self.assertEqual(self.seen, [])
        # the inbox turns it into one sentence to Alex and a done job, never "stopped with an error"
        from tabularium.core import agent_runtime
        src = Path(agent_runtime.__file__).read_text(encoding="utf-8")
        self.assertIn("except FileTooBig as exc:", src)
        # Telegram's own refusal, when the size was not announced, is the same sentence
        doc.document.file_size = None
        self.rt.bot.get_file = lambda fid: (_ for _ in ()).throw(RuntimeError("Error code: 400. Description: Bad Request: file is too big"))
        with self.assertRaises(FileTooBig):
            self.rt.handle_document(doc)


if __name__ == "__main__":
    unittest.main()
