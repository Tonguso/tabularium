"""Run explicitly with unittest; needs Playwright and Chromium, no live agent access."""
import json
import unittest
from pathlib import Path
from playwright.sync_api import sync_playwright, expect


class MobileDashboard(unittest.TestCase):
    def test_phone_navigation_retry_and_channel_display(self):
        root = Path(__file__).resolve().parents[1] / 'dashboard'
        runtime = sync_playwright().start()
        self.addCleanup(runtime.stop)
        browser = runtime.chromium.launch(args=['--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream'])
        self.addCleanup(browser.close)
        page = browser.new_page(viewport={'width': 393, 'height': 780}, is_mobile=True, has_touch=True)
        errors, posts = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        rows = [dict(id='in:1', role='user', text='Telegram-only text', channel='telegram', time=1, status='done'),
                dict(id='out:2', role='assistant', text='Dashboard question', channel='dashboard', time=2, status='sent', decision_id=9)]
        def route(request):
            path = request.request.url.split('8233', 1)[1].split('?', 1)[0]
            if path == '/api/messages' and request.request.method == 'POST':
                posts.append(request.request.post_data_json)
                if len(posts) in (1, 3):
                    request.abort('connectionreset')
                else:
                    request.fulfill(json={'job': 1, 'message_id': -1})
            elif path == '/api/attachments' and 'bad.heic' in request.request.url:
                request.fulfill(status=400,json={'error':'Unsupported attachment type'})
            elif path == '/api/attachments':
                request.fulfill(json={'id': 'a'*64+'.wav', 'name':'recording.wav', 'size':9, 'kind':'voice'})
            elif path == '/api/messages':
                request.fulfill(json={'ready': True, 'media_ready': True, 'messages': rows, 'note': 'Recent messages',
                                      'decisions': [{'id': 9, 'question': 'Dashboard question'}]})
            elif path == '/api/state':
                request.fulfill(json={'csrf': 'synthetic', 'agents': {name: {'status': 'ready'} for name in
                    ['cato', 'callimachus', 'vitruvius', 'tacitus']}, 'tasks': [], 'updated': 1})
            elif path.startswith('/api/'):
                request.fulfill(json={})
            else:
                file = root / ('index.html' if path == '/' else path.lstrip('/'))
                kinds = {'.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html', '.webmanifest': 'application/manifest+json'}
                if file.is_file():
                    request.fulfill(body=file.read_bytes(), content_type=kinds.get(file.suffix, 'image/png'))
                else:
                    request.fulfill(status=404)
        page.route('http://127.0.0.1:8233/**', route)
        manifest = json.loads((root / 'manifest.webmanifest').read_text())
        page.goto('http://127.0.0.1:8233' + manifest['start_url'])
        expect(page.locator('#messaging')).to_be_visible()
        expect(page.locator('#message-list')).to_contain_text('Dashboard question')
        expect(page.locator('#message-list')).not_to_contain_text('Telegram-only text')
        page.get_by_role('button', name='Reply to this question').click()
        page.locator('#message-text').fill('Yes, include the appendix')
        expect(page.locator('#message-send')).to_be_enabled()
        page.locator('#message-send').click()
        expect(page.locator('#message-send')).to_have_text('Retry send')
        page.reload()
        expect(page.locator('#message-text')).to_have_value('Yes, include the appendix')
        expect(page.locator('#message-send')).to_be_enabled()
        page.locator('#message-send').click()
        expect(page.locator('#message-text')).to_have_value('')
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0], posts[1])
        self.assertEqual(posts[0]['decision_id'], 9)
        page.locator('#message-file').set_input_files({'name':'recording.wav', 'mimeType':'audio/wav', 'buffer':b'RIFF demo'})
        expect(page.locator('#message-file-name')).to_contain_text('recording.wav')
        page.locator('#message-send').click()
        expect(page.locator('#message-send')).to_have_text('Retry send')
        page.reload()
        expect(page.locator('#message-file-name')).to_contain_text('recording.wav')
        expect(page.locator('#message-send')).to_be_enabled()
        page.locator('#message-send').click()
        expect(page.locator('#message-file-name')).to_have_text('')
        self.assertEqual(len(posts), 4)
        self.assertEqual(posts[2], posts[3])
        self.assertEqual(posts[2]['attachment']['name'], 'recording.wav')
        page.locator('#message-record').click()
        expect(page.locator('#message-record')).to_have_text('Stop recording')
        page.wait_for_timeout(1200)  # Record actual synthetic microphone samples before stopping.
        page.locator('#message-record').click()
        expect(page.locator('#message-file-name')).to_contain_text('voice.weba')
        page.reload()
        expect(page.locator('#message-file-name')).to_contain_text('voice.weba')
        page.locator('#message-file-remove').click()
        expect(page.locator('#message-file-name')).to_have_text('')
        self.assertEqual(len(posts), 4, 'Recording must not send itself')
        page.locator('#message-file').set_input_files({'name':'bad.heic', 'mimeType':'image/heic', 'buffer':b'not supported'})
        expect(page.locator('#message-file-name')).to_contain_text('bad.heic')
        page.locator('#message-send').click()
        expect(page.locator('#message-notice')).to_contain_text('Unsupported attachment type')
        expect(page.locator('#message-file-remove')).to_be_enabled()
        page.locator('#message-file-remove').click()
        expect(page.locator('#message-file-name')).to_have_text('')
        self.assertEqual(len(posts), 4, 'Rejected upload must not queue a message')
        page.locator('#message-text').fill('Unsent Cato draft')
        page.locator('#message-agent').select_option('vitruvius')
        expect(page.locator('#message-text')).to_have_value('')
        page.locator('#message-agent').select_option('cato')
        expect(page.locator('#message-text')).to_have_value('Unsent Cato draft')
        for height in (780, 430):
            page.set_viewport_size({'width': 393, 'height': height})
            box = page.locator('#message-send').bounding_box()
            self.assertLessEqual(box['y'] + box['height'], height)
            self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 393)
        page.set_viewport_size({'width': 393, 'height': 780})
        session = page.context.new_cdp_session(page)
        self.assertEqual(session.send('Page.getInstallabilityErrors')['installabilityErrors'], [])
        page.locator('#message-menu').click()
        page.locator('.nav[data-view="overview"]').click()
        expect(page.locator('#overview')).to_be_visible()
        page.go_back()
        expect(page.locator('#messaging')).to_be_visible()
        page.set_viewport_size({'width': 1365, 'height': 900})
        expect(page.locator('aside')).to_be_visible()
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
