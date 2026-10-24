"""Real tools -> Claude MCP handler -> OS pipe -> installed SDK JSON reader (no model/network)."""
import asyncio
import base64
import io
import json
import random
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
import claude_agent_sdk as sdk
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
from agent_harness import make_agent, put
from agent_core import wire_budget as wire
from agent_core.outcome import Failed
from agent_core.roster import ROLES
from background_runtime import TaskStore
import subscription_runtime as bridge


def noisy_png():
    image = Image.frombytes("L", (1500, 1500), random.Random(42).randbytes(1500 * 1500))
    buffer = io.BytesIO()
    try:
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    finally:
        image.close()
        buffer.close()


async def sdk_pipe(messages, options):
    """Echo the actual CLI frame shape through a subprocess and the real SDK reader."""
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    async def output():
        while True:
            chunk = await process.stdout.read(65536)
            if not chunk:
                break
            yield chunk.decode("ascii")
    async def write():
        process.stdin.write(("\n".join(json.dumps(m, ensure_ascii=True) for m in messages) + "\n").encode("ascii"))
        await process.stdin.drain()
        process.stdin.close()
    transport = SubprocessCLITransport(prompt="test", options=options)
    transport._process = process
    transport._stdout_stream = output()
    writer = asyncio.create_task(write())
    try:
        received = [m async for m in transport.read_messages()]
        await writer
        return received
    finally:
        if not writer.done():
            writer.cancel()
        if process.returncode is None:
            process.kill()
        await process.wait()


def echo(content):
    # Verified against the two 1,209,425-character task-16 CLI journal lines.
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "test", "content": content}]}, "toolUseResult": content}


class WireBudget(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.agent = make_agent("cato", self.root)

    def bridge_turn(self, calls):
        captured = {}
        def server(name, version, tools):
            captured["tools"] = {tool.name: tool for tool in tools}
            return {}
        async def query(prompt, options):
            captured["options"] = options
            captured["prompt"] = [m async for m in prompt]
            frames = []
            for name, args in calls:
                result = await captured["tools"][name].handler(args)
                captured.setdefault("results", []).append(result)
                frames.append(echo(result["content"]))
            frames.append({"type": "system", "subtype": "test_alive"})
            captured["received"] = await sdk_pipe(frames, options)
            yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                                    is_error=False, num_turns=1, session_id="offline",
                                    result="", structured_output={"reply": "Done"})
        patches = [patch.object(bridge, "find_cli", return_value="offline.exe"),
                   patch.object(bridge, "auth_status", return_value={"loggedIn": True, "authMethod": "claude.ai"}),
                   patch.object(sdk, "create_sdk_mcp_server", side_effect=server),
                   patch.object(sdk, "query", side_effect=query)]
        for replacement in patches:
            replacement.start()
        try:
            answer = bridge.run_claude(self.agent, [{"role": "user", "content": "Inspect the file"}], 1, "claude-test")
            self.assertEqual(answer, "Done")
        finally:
            for replacement in reversed(patches):
                replacement.stop()
        self.assertEqual(captured["options"].max_buffer_size, wire.message_budget())
        self.assertEqual(captured["received"][-1]["subtype"], "test_alive")
        return captured

    def test_two_megabyte_png_passes_real_tool_mcp_and_sdk_pipe(self):
        raw = noisy_png()
        self.assertTrue(2_000_000 < len(raw) < 2_500_000)
        path = self.root / "check_dual.png"
        path.write_bytes(raw)
        result = self.bridge_turn([("view_image", {"path": str(path)})])
        image = next(b for b in result["received"][0]["toolUseResult"] if b["type"] == "image")
        self.assertLessEqual(len(image["data"]), wire.IMAGE_ENCODED_CAP)
        decoded = Image.open(io.BytesIO(base64.b64decode(image["data"])))
        try:
            self.assertLessEqual(max(decoded.size), 1500)
            self.assertGreater(min(decoded.size), 500)
        finally:
            decoded.close()

    def test_oversized_result_is_archived_and_next_call_still_runs(self):
        original = self.agent._execute_tool_unguarded
        huge = "oversized\x00漢" * wire.TEXT_HEADROOM
        self.agent._execute_tool_unguarded = lambda name, args, chat: huge if name == "read_file" and args.get("path") == "huge" else original(name, args, chat)
        captured = self.bridge_turn([("read_file", {"path": "huge"}),
                                     ("read_file", {"path": str(self.root / "SKILL.md")})])
        self.assertTrue(captured["results"][0]["isError"])
        self.assertIn("rejected at the wire boundary", captured["results"][0]["content"][0]["text"])
        self.assertIn("Test identity", captured["results"][1]["content"][0]["text"])
        archives = list((self.root / "context_archive" / "results").glob("*.txt"))
        self.assertTrue(any(p.read_text(encoding="utf-8") == huge for p in archives))

    def test_max_images_and_unicode_fit_even_when_echoed_twice(self):
        images = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                   "data": "a" * wire.IMAGE_ENCODED_CAP}} for _ in range(wire.MAX_IMAGES)]
        self.assertEqual(wire.violation(images), "")
        self.assertLess(wire.json_size(echo(images)), wire.message_budget())
        received = asyncio.run(sdk_pipe([echo(images)], sdk.ClaudeAgentOptions(max_buffer_size=wire.message_budget())))
        self.assertEqual(len(received[0]["toolUseResult"]), wire.MAX_IMAGES)
        self.assertIsInstance(wire.bound_result(images + images[:1], self.root / "archive"), Failed)
        self.assertIsInstance(wire.bound_result("漢" * wire.TEXT_HEADROOM, self.root / "archive"), Failed)

    def test_photos_and_quoted_photos_use_the_same_encoder(self):
        from types import SimpleNamespace as NS
        raw = noisy_png()
        self.agent.ensure_initialised = lambda chat: None
        self.agent.bot = NS(get_file=lambda fid: NS(file_path="photo"), download_file=lambda path: raw)
        captured = []
        self.agent._run_image_turn = lambda chat, images, caption, sent: captured.extend(images)
        photo = NS(chat=NS(id=1), photo=[NS(file_id="1")], caption="test", date=1792318939, reply_to_message=None)
        self.agent.handle_photo([photo])
        self.assertLessEqual(len(captured[0][1]), wire.IMAGE_ENCODED_CAP)
        reference = NS(photo=photo.photo, date=photo.date, text=None, caption="test", voice=None, document=None, from_user=None)
        note, image = self.agent._reply_context(NS(reply_to_message=reference, quote=None))
        self.assertIsNotNone(image)
        self.assertLessEqual(len(image["source"]["data"]), wire.IMAGE_ENCODED_CAP)

    def test_failed_telegram_delivery_is_queued_and_sent_once_when_the_network_returns(self):
        # 2026-10-21: a notice during an outage is accepted into the durable outbox (not dropped, not raised), the
        # failure is acknowledged once, and the queued notice is delivered exactly once when Telegram is back.
        from types import SimpleNamespace as NS
        from unittest.mock import Mock
        self.agent.bot = NS(send_message=Mock(side_effect=OSError("offline")))
        self.assertTrue(self.agent.send_message(1, "Worker stopped"))
        self.assertEqual(len(self.agent.outbox().pending(1)), 1)
        store = TaskStore(self.root)
        task = store.start(1, "test delivery")
        put(store, task["id"], "interrupted", "Worker error: test")
        store.report_worker_failures(self.agent.send_message)
        store.report_worker_failures(self.agent.send_message)
        self.assertEqual(len(self.agent.outbox().pending(1)), 2)        # the notice, queued once
        self.agent.bot.send_message = Mock(return_value=NS(message_id=1))
        self.agent.outbox().flush()
        self.assertEqual(self.agent.bot.send_message.call_count, 2)        # "Worker stopped" + the notice
        self.assertEqual(self.agent.outbox().pending(), [])

    def test_uncompressible_image_fails_explicitly(self):
        replacement = patch("agent_core.shared_handlers.IMAGE_ENCODED_CAP", 1)
        replacement.start()
        try:
            path = self.root / "tiny.png"
            path.write_bytes(noisy_png())
            result = self.agent.tool_view_image(str(path))
        finally:
            replacement.stop()
        self.assertIsInstance(result, Failed)
        self.assertIn("cannot fit", str(result))


class CrashDelivery(unittest.TestCase):
    def test_delegated_failure_is_prompt_durable_and_deduplicated_across_retries(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        store = TaskStore(root, ROLES["vitruvius"])
        task = store.start(123, "View test screenshot")
        ident = task["id"]
        context = store.directory(ident) / "context.json"
        context.write_text(json.dumps({"context": "", "notify_telegram": False, "retries": 0}), encoding="utf-8")
        db = sqlite3.connect(store.database)
        try:
            db.execute("CREATE TABLE engineering_requests(task INTEGER)")
            db.execute("INSERT INTO engineering_requests VALUES (?)", (ident,))
            db.commit()
        finally:
            db.close()
        put(store, ident, "interrupted", "Worker error: CLIJSONDecodeError. Inspect worker.log.")
        notices = []
        def tick(current):
            current.launch_next = lambda: None
            current.poll_waiting = lambda: None
            current.last_monitor = current.last_delegate_poll = __import__("time").time()
            current.tick(lambda chat, text: notices.append((chat, text)))
        tick(store)
        self.assertEqual(len(notices), 1)
        self.assertIn("CLIJSONDecodeError", notices[0][1])
        self.assertIn(str(ident), notices[0][1])
        # Worker retries and a supervisor restart must not issue duplicate incident notices.
        put(store, ident, "running")
        put(store, ident, "interrupted", "Worker error: CLIJSONDecodeError.")
        tick(TaskStore(root, ROLES["vitruvius"]))
        self.assertEqual(len(notices), 1)
        store.control(123, ident, "resume")
        put(store, ident, "interrupted", "Worker error: a new incident.")
        tick(store)
        self.assertEqual(len(notices), 2)

    def test_real_worker_exception_reaches_monitor_even_for_delegate(self):
        from test_worker import STUB
        import time
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        stub = (STUB + "\n_routing = NS(enabled=lambda root: True)\n").replace('os.environ["CATO_BACKGROUND_TASK"]', 'os.environ["VITRUVIUS_BACKGROUND_TASK"]')
        stub = stub.replace('raise RunawayStop("5 consecutive tool calls failed or were refused; the last was review_output")',
                            'raise RuntimeError("test worker exception")')
        (root / "telegram.py").write_text(stub, encoding="utf-8")
        store = TaskStore(root, ROLES["vitruvius"])
        task = store.start(123, "Inspect screenshot")
        context = store.directory(task["id"]) / "context.json"
        context.write_text(json.dumps({"context": "", "notify_telegram": False, "retries": 0}), encoding="utf-8")
        notices = []
        store.launch_next()
        process = store.processes[task["id"]]  # This test owns the child until teardown completes.
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                store.report_worker_failures(lambda chat, text: notices.append(text))
                if notices:
                    break
                time.sleep(0.05)
            self.assertEqual(len(notices), 1)
            self.assertIn("RuntimeError", notices[0], (store.directory(task["id"]) / "worker.log").read_text(encoding="utf-8"))
            self.assertEqual(store.get(task["id"])["status"], "interrupted")
            store.report_worker_failures(lambda chat, text: notices.append(text))
            self.assertEqual(len(notices), 1)
        finally:
            try:
                process.wait(timeout=5)  # Failure is recorded before worker shutdown has finished.
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)

    def test_failed_send_remains_pending(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = TaskStore(Path(tmp.name))
        task = store.start(1, "test")
        put(store, task["id"], "interrupted", "Worker error: exception")
        def unavailable(*args):
            raise OSError("offline")
        self.assertRaises(OSError, store.report_worker_failures, unavailable)
        notices = []
        store.report_worker_failures(lambda *args: notices.append(args))
        store.report_worker_failures(lambda *args: notices.append(args))
        self.assertEqual(len(notices), 1)


if __name__ == "__main__":
    unittest.main()
