"""Uploads use temporary agent folders; no provider, Telegram or live state access."""
import io
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from tabularium.channels import messaging
from tabularium.core.agent_runtime import DurableInbox
from tabularium.channels.owner_telegram import OWNER_CHAT_ID


class DashboardMedia(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_upload_is_immutable_and_retry_is_one_queue_item(self):
        data = b'RIFF synthetic audio'
        ref = messaging.store_attachment(self.root, 'voice.wav', io.BytesIO(data), len(data))
        again = messaging.store_attachment(self.root, 'voice.wav', io.BytesIO(data), len(data))
        self.assertEqual(ref, again)
        self.assertEqual(messaging.attachment_path(self.root, ref).read_bytes(), data)
        inbox = DurableInbox(self.root, Mock())
        receipt = inbox.accept_dashboard('media-1', 'Use this too', attachment=ref)
        self.assertEqual(receipt, inbox.accept_dashboard('media-1', 'Use this too', attachment=ref))
        self.assertRaises(ValueError, inbox.accept_dashboard, 'media-1', 'Use this too')
        db = sqlite3.connect(inbox.database)
        try:
            rows = db.execute('SELECT handler,payload FROM jobs').fetchall()
        finally:
            db.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 'handle_dashboard_media')
        decoded = messaging.decode(json.loads(rows[0][1]))
        self.assertEqual(decoded.caption, 'Use this too')
        self.assertEqual(decoded.attachment, ref)

    def test_invalid_or_partial_upload_never_enters_queue(self):
        for filename, size in [('../escape.mp4', 2), ('program.exe', 2), ('video.mp4', 0),
                               ('video.mp4', messaging.MAX_ATTACHMENT_BYTES + 1)]:
            self.assertRaises(ValueError, messaging.store_attachment, self.root, filename, io.BytesIO(b'xx'), size)
        self.assertRaises(ValueError, messaging.store_attachment, self.root, 'short.mp4', io.BytesIO(b'x'), 2)
        self.assertFalse(list((self.root/'media/dashboard').glob('*')))
        inbox = DurableInbox(self.root, Mock())
        self.assertRaises(ValueError, inbox.accept_dashboard, 'x', '', attachment={'id': '../../memory.md', 'name': 'x.mp4'})

    def test_large_audio_is_rejected_before_reading_any_bytes(self):
        stream = Mock()
        stream.read.side_effect = AssertionError('Oversized voice reached the reader')
        self.assertRaises(ValueError, messaging.store_attachment, self.root, 'voice.wav', stream, 26*1024*1024)

    def test_media_uses_real_agent_image_and_voice_paths_for_every_agent(self):
        from tests.agent_harness import make_agent, AGENTS
        from PIL import Image
        from tabularium.core.reply_envelope import Reply
        image = Image.new('RGB', (30, 30), 'red')
        buf = io.BytesIO()
        image.save(buf, 'PNG')
        image.close()
        for name in AGENTS:
            root = self.root/name
            root.mkdir()
            agent = make_agent(name, root)
            agent._inbox = DurableInbox(root, Mock())
            agent.bot = Mock()
            agent.bot.send_chat_action.side_effect = AssertionError('Dashboard must not call Telegram')
            agent.bot.download_file.side_effect = AssertionError('Local upload must not call Telegram')
            agent._run_turn_with_guard = Mock(return_value=Reply('Synthetic reply'))
            agent.send_message = Mock()
            agent._speak = Mock(side_effect=AssertionError('Dashboard must not send Telegram voice'))
            for filename, data in [('photo.png', buf.getvalue()), ('voice.wav', b'RIFF test')]:
                ref = messaging.store_attachment(root, filename, io.BytesIO(data), len(data))
                receipt = agent._inbox.accept_dashboard(filename, 'Additional requirement', attachment=ref)
                agent._inbox.local.message_id = receipt['message_id']
                msg = messaging.decode({'channel': 'dashboard', 'message_id': receipt['message_id'],
                    'date': 1, 'text': 'Additional requirement', 'attachment': ref})
                client = Mock()
                client.audio.transcriptions.create.return_value.text = 'Remember the appendix'
                patcher = patch('openai.OpenAI', return_value=client)
                patcher.start()
                try:
                    agent.handle_dashboard_media(msg)
                finally:
                    patcher.stop()
                self.assertTrue(agent._run_turn_with_guard.called)
                sent = agent._run_turn_with_guard.call_args.args[0]
                self.assertIn('Additional requirement', str(sent))
                if filename.endswith('.wav'):
                    self.assertIn('Remember the appendix', str(sent))
                    self.assertEqual(client.audio.transcriptions.create.call_args.kwargs['file'].name, 'voice.wav')
                agent._run_turn_with_guard.reset_mock()
        buf.close()

    def test_real_video_frames_reach_each_agents_model_without_telegram(self):
        from tests.agent_harness import make_agent, AGENTS
        from tabularium.tools import video_intake
        import threading
        from tabularium.core.reply_envelope import Reply
        source = self.root/'clip.mp4'
        ffmpeg, _ = video_intake.ffmpeg_paths()
        subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                        'color=c=red:s=160x120:d=1', '-an', str(source)], check=True,
                       capture_output=True, timeout=20, creationflags=0x08000000)
        data = source.read_bytes()
        for name in AGENTS:
            root = self.root/name
            root.mkdir()
            agent = make_agent(name, root)
            agent._inbox = DurableInbox(root, Mock())
            agent.bot = Mock()
            agent.bot.send_chat_action.side_effect = AssertionError('No Telegram for local video')
            agent._run_turn_with_guard = Mock(return_value=Reply('Video reply'))
            agent.send_message = Mock()
            ref = messaging.store_attachment(root, 'clip.mp4', io.BytesIO(data), len(data))
            finished = threading.Event()
            agent.send_message.side_effect = lambda chat, text: finished.set() if str(text) == 'Video reply' else None
            agent.register_handlers()
            agent._inbox.accept_dashboard('video', 'Find the red frame', attachment=ref)
            agent._inbox.start()
            try:
                self.assertTrue(finished.wait(15), name + ' did not consume its queued video')
            finally:
                agent._inbox.close()
            messages = agent._run_turn_with_guard.call_args.args[0]
            blocks = messages[-1]['content']
            self.assertTrue(any(b.get('type') == 'image' for b in blocks))
            self.assertIn('Find the red frame', str(blocks))
            self.assertIn('no audio track', str(blocks))

    def test_http_upload_auth_size_and_capability_gate(self):
        import importlib
        import http.client
        import threading
        from http.server import ThreadingHTTPServer
        # Import-time task managers must never open the real agent databases in this test.
        constructor = patch('tabularium.records.background_runtime.TaskStore', return_value=Mock())
        constructor.start()
        try:
            dashboard = importlib.import_module('tabularium.dashboard.server')
        finally:
            constructor.stop()
        patches = [patch.object(dashboard, 'ROOT', self.root), patch.object(dashboard.messaging, 'ready', return_value=True)]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        server = ThreadingHTTPServer(('127.0.0.1', 0), dashboard.Handler)
        runner = threading.Thread(target=server.serve_forever, daemon=True)
        runner.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        self.addCleanup(connection.close)
        headers = {'Host':f'127.0.0.1:{dashboard.PORT}', 'Origin':dashboard.URL,
                   'Content-Type':'application/octet-stream'}
        connection.request('POST', '/api/attachments?agent=coordinator&name=voice.wav', b'RIFF demo', headers)
        response = connection.getresponse()
        self.assertEqual(response.status, 403)
        response.read()
        headers['X-CSRF-Token'] = dashboard.TOKEN
        connection.request('POST', '/api/attachments?agent=coordinator&name=voice.wav', b'RIFF demo', headers)
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        ref = json.loads(response.read())
        self.assertEqual(messaging.attachment_path(self.root/'coordinator', ref).read_bytes(), b'RIFF demo')
        patches[-1].target.ready.return_value = False
        connection.request('POST', '/api/attachments?agent=coordinator&name=voice.wav', b'RIFF demo', headers)
        response = connection.getresponse()
        self.assertEqual(response.status, 400)
        response.read()


if __name__ == '__main__':
    unittest.main()
