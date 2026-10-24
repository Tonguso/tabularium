"""Offline checks for manual-only allowance refresh and hidden CLI launches."""
import asyncio
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import subscription_usage as usage


class SubscriptionUsage(unittest.TestCase):
    def test_only_manual_refresh_starts_worker_and_deduplicates(self):
        with patch.object(usage, 'read_cache', return_value={'checked': 0}), \
             patch.object(usage, 'LOCK', threading.Lock()), \
             patch.object(usage, 'LAST_ATTEMPT', 0), \
             patch.object(usage.threading, 'Thread') as worker:
            for _ in range(3):
                self.assertFalse(usage.snapshot()['refreshing'])
            worker.assert_not_called()
            self.assertTrue(usage.snapshot(force=True)['refreshing'])
            usage.snapshot(force=True)
            usage.snapshot()
            worker.assert_called_once()
            worker.return_value.start.assert_called_once()

    def test_sdk_launches_hidden_and_loop_restored_on_success_and_error(self):
        async def check(fail):
            loop = asyncio.get_running_loop()
            start = AsyncMock()

            class Client:
                def __init__(self, **kwargs):
                    self._query = self

                async def __aenter__(self):
                    await loop.subprocess_exec(None, 'claude', '-v', creationflags=0)
                    await loop.subprocess_exec(None, 'claude', '--sdk', creationflags=0)
                    if fail:
                        raise RuntimeError('test failure')
                    return self

                async def __aexit__(self, *args):
                    pass

                async def _send_control_request(self, request):
                    self_request = {'subtype': 'get_usage', 'skip_behaviors': True}
                    assert request == self_request
                    return {'rate_limits_available': True}

            with patch.object(loop, 'subprocess_exec', start), \
                 patch.object(usage, 'find_cli', return_value='claude'), \
                 patch('claude_agent_sdk.ClaudeSDKClient', Client):
                if fail:
                    with self.assertRaises(RuntimeError):
                        await usage.claude_usage()
                else:
                    self.assertTrue((await usage.claude_usage())['available'])
                self.assertIs(loop.subprocess_exec, start)
                self.assertEqual(start.await_count, 2)
                for call in start.await_args_list:
                    self.assertEqual(call.kwargs['creationflags'], subprocess.CREATE_NO_WINDOW)

        asyncio.run(check(False))
        asyncio.run(check(True))


if __name__ == '__main__':
    unittest.main()
