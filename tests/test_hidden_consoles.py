"""Background commands must not allocate a Windows console."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
from agent_harness import load_agent
from tabularium.models import subscription_runtime
@unittest.skipUnless(os.name == 'nt', 'Windows console behavior')
class HiddenConsoles(unittest.TestCase):
    def test_shell_tool_requests_no_console(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'cato'; root.mkdir()
            runtime = load_agent('cato', root)['runtime']
            runtime.BACKGROUND_TASK_ID = ''
            runtime.auto_modes[1] = True
            with patch('tabularium.core.agent_app.subprocess.Popen') as launch:
                launch.return_value.communicate.return_value = (b'hello', b'')
                launch.return_value.returncode = 0
                self.assertEqual(json.loads(runtime.tool_execute_command('Write-Output hello', 1))['stdout'], 'hello')
                self.assertTrue(launch.call_args.kwargs['creationflags'] & subprocess.CREATE_NO_WINDOW)

    def test_sdk_loop_child_has_no_console_and_keeps_output(self):
        async def run(caller):
            child = await asyncio.create_subprocess_exec(
                sys.executable, '-c', 'import ctypes; print(ctypes.windll.kernel32.GetConsoleWindow())',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
            out, err = await child.communicate()
            self.assertEqual(child.returncode, 0, err)
            return out.strip()
        self.assertEqual(subscription_runtime._run_coroutine(run), b'0')

if __name__ == '__main__': unittest.main()