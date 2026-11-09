"""Offline tests for Telegram video intake; no bot, network or whisper calls."""
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

AGENT_DIR = Path(__file__).resolve().parent.parent
from tabularium.tools import video_intake as vi
class FakeBot:
    def __init__(self, error=None, data=b"video"):
        self.error, self.data, self.calls = error, data, []

    def get_file(self, file_id):
        self.calls.append(file_id)
        if self.error:
            raise self.error
        return NS(file_path="videos/file_1.mp4")

    def download_file(self, path):
        return self.data


class SizeLimit(unittest.TestCase):
    def test_over_limit_never_calls_get_file(self):
        bot = FakeBot()
        ref = NS(file_id="A", file_unique_id="U", file_size=vi.MAX_BYTES + 1)
        with self.assertRaises(vi.TooBig):
            vi.download(bot, ref)
        self.assertEqual(bot.calls, [])

    def test_telegram_file_is_too_big_error_maps_to_toobig(self):
        bot = FakeBot(error=Exception("A request to the Telegram API was unsuccessful. "
                                      "Error code: 400. Description: Bad Request: file is too big"))
        ref = NS(file_id="A", file_unique_id="U", file_size=None)
        with self.assertRaises(vi.TooBig):
            vi.download(bot, ref)

    def test_other_errors_propagate(self):
        with self.assertRaises(RuntimeError):
            vi.download(FakeBot(error=RuntimeError("network")), NS(file_id="A", file_size=10))

    def test_limit_boundary_and_message(self):
        self.assertFalse(vi.too_big(vi.MAX_BYTES))
        self.assertTrue(vi.too_big(vi.MAX_BYTES + 1))
        self.assertFalse(vi.too_big(None))
        self.assertIn("20 MB", vi.TOO_BIG_TEXT)
        self.assertIn("local folder", vi.TOO_BIG_TEXT)

    def test_download_path_naming(self):
        with tempfile.TemporaryDirectory() as tmp:
            ref = NS(file_id="A", file_unique_id="AgAD-x1", file_size=5, mime_type="video/quicktime")
            path = vi.download(FakeBot(), ref, sent=1791273600, directory=tmp)
            self.assertRegex(path.name, r"^\d{4}-\d{2}-\d{2}_AgAD-x1\.mov$")
            self.assertEqual(path.read_bytes(), b"video")


class MessageTypes(unittest.TestCase):
    def test_accepted_types(self):
        for kind in ("video", "video_note", "animation"):
            ref = NS(file_id=kind)
            self.assertEqual(vi.video_ref(NS(**{kind: ref})), (ref, kind))
        doc = NS(file_id="d", mime_type="video/mp4")
        self.assertEqual(vi.video_ref(NS(document=doc)), (doc, "document"))
        self.assertIsNone(vi.video_ref(NS(document=NS(file_id="p", mime_type="application/pdf"))))
        self.assertIsNone(vi.video_ref(NS(text="hello")))

    def test_video_documents_reach_the_video_handler_first(self):
        """Registration on the real agent: the video handler (registered by Cato's hook before the generic document
        handler) is first in line, and its filter takes a video sent as a document but not a PDF."""
        import tempfile
        from agent_harness import make_agent
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "coordinator"; root.mkdir()
            agent = make_agent("coordinator", root)
            agent._inbox.handler = lambda fn=None, album=False: fn if fn is not None else (lambda g: g)   # leave handlers recognisable
            agent.register_handlers()
            handlers = [h["function"] for h in agent.bot.message_handlers]
            video = handlers.index(agent.handle_video)
            document = handlers.index(agent.handle_document)
            self.assertLess(video, document)
            accepts = agent.bot.message_handlers[video]["filters"]["func"]
            self.assertTrue(accepts(NS(document=NS(file_id="d", mime_type="video/mp4"))))
            self.assertFalse(accepts(NS(document=NS(file_id="p", mime_type="application/pdf"))))

class FrameSelection(unittest.TestCase):
    def test_floor_fills_gaps_without_scene_changes(self):
        times = vi.select_frames([], 35)
        self.assertEqual(times[0], 0.0)
        gaps = [b - a for a, b in zip(times, times[1:] + [35])]
        self.assertTrue(all(g <= vi.FRAME_INTERVAL + 1e-9 for g in gaps), times)
        self.assertEqual(len(times), 4)

    def test_scene_changes_kept_and_near_duplicates_merged(self):
        times = vi.select_frames([8.0, 8.2, 15.0], 20)
        self.assertIn(8.0, times)
        self.assertNotIn(8.2, times)
        self.assertIn(15.0, times)
        self.assertEqual(times, sorted(times))

    def test_cap_thins_evenly_and_keeps_ends(self):
        scenes = [i * 0.9 for i in range(1, 200)]
        times = vi.select_frames(scenes, 200)
        self.assertEqual(len(times), vi.MAX_FRAMES)
        self.assertEqual(times[0], 0.0)
        self.assertEqual(times[-1], max(vi.select_frames(scenes, 200, cap=10_000)))

    def test_out_of_range_and_unknown_duration(self):
        self.assertEqual(vi.select_frames([-1, 50], 30)[0], 0.0)
        self.assertNotIn(50, vi.select_frames([-1, 50], 30))
        self.assertEqual(vi.select_frames([], 0), [0.0])

    def test_pick_attachments(self):
        frames = list(range(24))
        picked = vi.pick_attachments(frames)
        self.assertEqual(len(picked), vi.ATTACH_FRAMES)
        self.assertEqual((picked[0], picked[-1]), (0, 23))
        self.assertEqual(vi.pick_attachments([1, 2]), [1, 2])


class NoAudio(unittest.TestCase):
    def test_no_audio_skips_transcription(self):
        with tempfile.TemporaryDirectory() as tmp:
            frame = Path(tmp) / "f.jpg"
            with patch.object(vi, "ffmpeg_paths", return_value=("ffmpeg", "ffprobe")), \
                 patch.object(vi, "probe", return_value=(12.0, False, True)), \
                 patch.object(vi, "scene_times", return_value=[]), \
                 patch.object(vi, "extract_frames", return_value=[(0.0, frame)]), \
                 patch.object(vi, "contact_sheet", return_value=frame), \
                 patch.object(vi, "transcribe") as transcribe:
                result = vi.process_video(Path(tmp) / "clip.mp4")
            transcribe.assert_not_called()
            self.assertIsNone(result["transcript"])
            self.assertIn("no audio track", vi.describe(result))

    @unittest.skipUnless(Path(vi._FFMPEG_FALLBACK).exists() or vi.shutil.which("ffmpeg"), "ffmpeg missing")
    def test_real_silent_clip(self):
        ffmpeg, _ = vi.ffmpeg_paths()
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mp4"
            subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=10",
                            "-pix_fmt", "yuv420p", str(clip)], check=True, timeout=60)
            with patch.object(vi, "transcribe") as transcribe:
                result = vi.process_video(clip, total_seconds=120)
            transcribe.assert_not_called()
            self.assertFalse(result["has_audio"])
            self.assertTrue(result["sheet"].exists())
            self.assertGreaterEqual(len(result["frames"]), 1)


if __name__ == "__main__":
    unittest.main()
