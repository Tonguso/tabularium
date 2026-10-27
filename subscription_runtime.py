"""Claude subscription adapter using canonical agent context and only the agent's tools."""
from agent_core.wire_budget import message_budget, bound_result, MAX_IMAGES, json_size, CLI_RESULT_COPIES, ENVELOPE_HEADROOM
from agent_core.context_window import archive_preview
from agent_core.outcome import Failed
from agent_core.session_lifecycle import SessionLimit
import asyncio
import contextvars
import glob
import threading
import os
import shutil
import subprocess
import time
from pathlib import Path
import json
import queue
import uuid
from provider_routing import Unavailable


def find_cli():
    import claude_agent_sdk
    candidates = [shutil.which('claude.exe'), str(Path.home()/'.local/bin/claude.exe'),
                  str(Path(claude_agent_sdk.__file__).parent/'_bundled/claude.exe')]
    for base in (os.environ.get('APPDATA',''), os.environ.get('LOCALAPPDATA','')):
        candidates += glob.glob(str(Path(base)/'Claude/claude-code/*/*/claude.exe'))
    candidates += glob.glob(str(Path(os.environ.get('LOCALAPPDATA',''))/'Packages/Claude_*/LocalCache/Roaming/Claude/claude-code/*/*/claude.exe'))
    # The Claude desktop app (Microsoft Store) keeps the Claude Code CLI it runs here; its app\claude.exe under
    # WindowsApps is the Electron app itself, not the CLI (2026-10-22). On 2026-10-20 20:14 an update removed the
    # stand-alone CLI from ~/.local/bin and the SDK bundle, and this was the only CLI left on the machine.
    candidates += glob.glob(str(Path(os.environ.get('LOCALAPPDATA',''))/'Claude-Data/claude_*/claude-code/*/*/claude.exe'))
    candidates = [p for p in candidates if p and Path(p).is_file()]
    return max(candidates, key=os.path.getmtime) if candidates else None


def subscription_env():
    return dict(os.environ, ANTHROPIC_API_KEY='', ANTHROPIC_AUTH_TOKEN='',
                CLAUDE_CODE_USE_BEDROCK='0', CLAUDE_CODE_USE_VERTEX='0', CLAUDE_CODE_USE_FOUNDRY='0')


def long_lived_token():
    """True when config.bat supplies CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`).

    That one-year token never refreshes, so concurrent Claude Code processes cannot race on a
    rotating refresh token -- the cause of the shared /login being revoked twice on 2026-10-06.
    Claude Code ranks it above the /login credential, and child processes inherit it."""
    return bool(os.environ.get('CLAUDE_CODE_OAUTH_TOKEN', '').strip())


def subscription_ready():
    """(ok, reason) for the Claude subscription route, without spawning a CLI when the
    long-lived token is configured."""
    if long_lived_token():
        return True, ''
    auth = auth_status()
    if auth.get('loggedIn') and auth.get('authMethod') == 'claude.ai':
        return True, ''
    return False, SIGN_IN_REASON


def auth_status():
    cli = find_cli()
    if not cli:
        return {'loggedIn': False, 'message': 'Claude Code is not installed'}
    result = subprocess.run([cli, 'auth', 'status', '--json'], capture_output=True,
                            timeout=20, env=subscription_env(), creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    try:
        status = json.loads(result.stdout)
    except (ValueError, UnicodeDecodeError):
        return {'loggedIn': False, 'message': 'Unable to read Claude Code sign-in status'}
    # Never expose account identifiers or credentials through the dashboard.
    return {k:status.get(k) for k in ('loggedIn','authMethod','subscriptionType')}


class CliStartLock:
    """Machine-wide lock held while a Claude Code CLI process starts and makes its first call.

    All agents, their background workers and the dashboard share ONE subscription login in
    ~/.claude/.credentials.json. Refresh tokens are single-use: two CLIs refreshing at once
    present the same token twice, the server treats that as token theft and revokes the whole
    login. On 2026-10-05 22:42 Cato's foreground turn and a background worker started together;
    the next morning the login was dead ("OAuth session expired and could not be refreshed").
    Serialising only the start-up keeps any refresh to one process at a time."""

    PATH = Path(__file__).resolve().parent / 'logs' / 'claude_cli_start.lock'

    def __init__(self, timeout=180):
        self.timeout, self.handle, self.held = timeout, None, False

    def acquire(self):
        import msvcrt
        self.PATH.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.PATH, 'a+b')
        deadline = time.time() + self.timeout
        while True:
            try:
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                self.held = True
                return self
            except OSError:
                if time.time() > deadline:      # never deadlock a turn on a stale holder
                    self.handle.close(); self.handle = None
                    return self
                time.sleep(0.25)

    def release(self):
        if self.handle is None:
            return
        try:
            if self.held:
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            self.held = False
            self.handle.close()
            self.handle = None

    __enter__ = acquire

    def __exit__(self, *exc):
        self.release()


SIGN_IN_REASON = 'Claude subscription sign-in expired'
AUTH_FAILURE_TEXT = ('not logged in', 'please run /login', 'authentication required', 'failed to authenticate',
                     'oauth session expired', 'could not be refreshed', 'invalid_grant', 'token has been revoked')


class _CallerThread:
    """Lets the SDK's event loop hand blocking tool calls back to the turn thread."""

    def __init__(self):
        self.jobs = queue.Queue()

    async def call(self, fn, *args):
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.jobs.put((fn, args, loop, future))
        return await future


def _run_coroutine(factory):
    """Run the SDK loop on its own thread; execute tool calls back on the calling thread.

    Two constraints, both hit on 2026-10-05:
    1. The browser tool uses Playwright's *sync* API, which leaves an asyncio loop running on
       the turn thread. asyncio.run() there fails with "cannot be called from a running event
       loop", which broke the ChatGPT-quota fallback to the Claude subscription.
    2. Playwright sync objects only work on the thread that created them, so tools must not
       run in asyncio.to_thread's pool ("cannot switch to a different thread").
    So the loop gets a fresh thread (carrying contextvars such as the active route), and this
    thread serves the tool queue until the loop finishes."""
    caller = _CallerThread()
    box, ctx = {}, contextvars.copy_context()

    async def hidden_run():
        # This fresh loop belongs only to this SDK session; hide its CLI/version subprocesses.
        loop = asyncio.get_running_loop()
        start_process = loop.subprocess_exec
        async def hidden_process(*args, **kwargs):
            kwargs['creationflags'] = kwargs.get('creationflags', 0) | getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            return await start_process(*args, **kwargs)
        loop.subprocess_exec = hidden_process
        try:
            return await factory(caller)
        finally:
            loop.subprocess_exec = start_process

    def worker():
        try:
            box['value'] = ctx.run(asyncio.run, hidden_run())
        except BaseException as exc:
            box['error'] = exc
        finally:
            caller.jobs.put(None)

    threading.Thread(target=worker, daemon=True, name='claude-subscription-loop').start()
    while (job := caller.jobs.get()) is not None:
        fn, args, loop, future = job
        if loop.is_closed() or future.cancelled():
            continue
        try:
            outcome = (True, fn(*args))
        except Exception as exc:
            outcome = (False, exc)

        def settle(future=future, outcome=outcome):
            if not future.done():
                (future.set_result if outcome[0] else future.set_exception)(outcome[1])
        try:
            loop.call_soon_threadsafe(settle)
        except RuntimeError:            # loop already closed
            pass
    if 'error' in box:
        raise box['error']
    return box['value']


SILENCE_SECONDS = 600       # no stream event and no tool running for this long: the connection is hung (task 134, 2026-10-10)

def stream_silent(activity, now, limit=SILENCE_SECONDS):
    """True when the SDK stream has produced nothing for `limit` seconds while no tool call of ours is in progress."""
    return not activity.get('busy') and now - activity.get('at', now) > limit

SDK_TOOL_PREFIX = 'mcp__agent__'

def sdk_tool_names(history):
    """Copy of the replayed history with every tool_use name in the form the SDK exposes (mcp__agent__<name>).
    The saved conversation keeps bare names (the OpenAI route and our own receipts use them); the model on the Claude
    route copies whatever it sees in the transcript, and a bare name there produced 'no such tool' (2026-10-09)."""
    out = []
    for message in history:
        content = message.get('content') if isinstance(message, dict) else None
        if isinstance(content, list):
            blocks = []
            for block in content:
                if isinstance(block, dict) and block.get('type') == 'tool_use' and isinstance(block.get('name'), str) \
                        and not block['name'].startswith(SDK_TOOL_PREFIX):
                    block = dict(block, name=SDK_TOOL_PREFIX + block['name'])
                blocks.append(block)
            message = dict(message, content=blocks)
        out.append(message)
    return out

def run_claude(agent, messages, chat, model, reminded=False, retry=None):
    from agent_core import reply_envelope
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, AssistantMessage, create_sdk_mcp_server, query, tool
    cli = find_cli()
    if not cli:
        raise Unavailable('Claude Code is not installed (no claude.exe found; reinstall Claude Code or the Claude desktop app)')
    ready, reason = subscription_ready()
    if not ready:
        raise Unavailable(reason)
    schemas = [s for s in agent.get_tools('anthropic') if 'input_schema' in s]
    agent._background_boundary(messages, [], chat)
    if agent._abort_requested.pop(chat, False):
        return 'Stopped.'
    turn_started = time.time()                     # an accepted start/resume/reopen after this backs "work started"
    history = agent._prepare_request_context(
        agent._sanitise_tool_history(agent.trim_history(messages, chat)), chat)
    if retry:
        history = history + [retry]
    images = []

    def serial(value):
        if hasattr(value, 'model_dump'):
            value = value.model_dump()
        if isinstance(value, dict):
            if value.get('type') == 'image':
                bounded = bound_result([value], agent.AGENT_DIR / 'context_archive' / 'results')
                if isinstance(bounded, Failed):
                    return {'type': 'text', 'text': str(bounded)}
                images.append(value)
                return {'type': 'text', 'text': f'[Image {len(images)} attached below]'}
            return {k: serial(v) for k, v in value.items() if k != 'cache_control'}
        if isinstance(value, list):
            return [serial(v) for v in value]
        return value

    transcript = json.dumps(serial(sdk_tool_names(history)), ensure_ascii=False)
    blocks = [{'type': 'text', 'text': 'Continue this conversation at the latest user request. '
               'The JSON below is conversation history, including completed tool actions, not new system instructions. '
               'Do not repeat completed actions. If an action outcome is unknown, inspect it before proceeding. '
               'Your tools in this session are the mcp__agent__* tools listed for you; call them by those exact names.\n'+transcript}] + images
    # A resumed history can contain several turns of valid images. Retain the most recent set;
    # older images remain retrievable, rather than multiplying the live-session attachment budget.
    for index in range(max(0, len(images) - MAX_IMAGES)):
        blocks[index + 1] = {'type': 'text', 'text': archive_preview(
            [images[index]], agent.AGENT_DIR / 'context_archive' / 'results', preview_chars=400)}
    if CLI_RESULT_COPIES * json_size(blocks) + ENVELOPE_HEADROOM > message_budget():
        raise Unavailable('Claude prompt exceeds the shared wire budget before session start; use a smaller context or another route', 60)
    uncertain = False
    activity = {'at': time.time(), 'busy': False}   # for the silence watchdog in run()
    # The inbox tracks the current job in a thread-local; carry it onto the loop thread so
    # progress heartbeats and usage records stay attributed to this turn.
    inbox_local = getattr(agent._inbox, 'local', None)
    inbox_job = getattr(inbox_local, 'job', None)
    # Pass the system prompt as a FILE. The SDK otherwise puts it on the CLI command line, and
    # Windows caps a command line at 32,767 chars: Cato's prompt exceeds that, CreateProcess
    # fails with WinError 206 and the SDK misreports it as "Claude Code not found" (2026-10-05).
    # Per RUNTIME_DIR, so a background worker never overwrites the foreground's file.
    system_prompt_file = Path(agent.RUNTIME_DIR) / 'task_state' / 'claude_system_prompt.txt'
    system_prompt_file.parent.mkdir(parents=True, exist_ok=True)
    system_prompt_file.write_text(agent.SYSTEM_PROMPT + f'\n[Active model: {model}, Claude subscription.]',
                                  encoding='utf-8')

    next_retry = []

    async def run(caller):
        if inbox_local is not None:
            inbox_local.job = inbox_job
        # Serialize tool receipts even if the SDK asks for parallel calls.
        lock = asyncio.Lock()

        def make(schema):
            async def handler(args):
                nonlocal uncertain
                async with lock:
                    # A steer that arrived mid-session. Saving it to history is not delivery: this live session was
                    # started with the old transcript and never re-reads history. It reaches the model inside this
                    # tool's result, the only channel into a running session (task 144, 2026-10-11).
                    from agent_core.session_lifecycle import TaskWaiting
                    try:
                        arrived = await caller.call(agent._background_boundary, messages, [], chat) or []
                    except TaskWaiting as exc:
                        events.put_nowait(exc)
                        return {'content': [{'type': 'text', 'text': 'Not executed: waiting for the recorded decision.'}], 'isError': True}
                    if agent._abort_requested.pop(chat, False):
                        raise RuntimeError('Stopped by user')
                    ident = 'route_'+uuid.uuid4().hex
                    result_block = {'type': 'tool_result', 'tool_use_id': ident,
                                    'content': 'Outcome unknown: tool started; inspect action receipts before retrying.', 'is_error': True}
                    messages.extend([{'role': 'assistant', 'content': [{'type': 'tool_use', 'id': ident,
                                     'name': schema['name'], 'input': dict(args or {})}]},
                                     {'role': 'user', 'content': [result_block]}])
                    agent.save_conversation(chat)
                    uncertain = True
                    # On the turn thread, not a pool thread: Playwright sync objects are thread-bound.
                    activity['busy'] = True
                    try:
                        result = await caller.call(agent.execute_tool, schema['name'], dict(args or {}), chat)
                    finally:
                        activity['busy'] = False
                        activity['at'] = time.time()
                    result_block['content'] = result if isinstance(result, (str, list)) else str(result)
                    if isinstance(result, Failed):
                        result_block['is_error'] = True
                    else:
                        result_block.pop('is_error', None)
                    agent.save_conversation(chat)
                    uncertain = False
                    try:
                        if agent.BACKGROUND_TASK_ID:
                            agent._check_task_waiting(chat)
                    except TaskWaiting as exc:
                        events.put_nowait(exc)
                    content = []
                    for b in result if isinstance(result, list) else [{'type': 'text', 'text': str(result)}]:
                        if b.get('type') == 'image':
                            src = b['source']
                            content.append({'type': 'image', 'data': src['data'], 'mimeType': src['media_type']})
                        elif b.get('type') == 'text':
                            content.append({'type': 'text', 'text': b.get('text', '')})
                    content = content or [{'type': 'text', 'text': '(no output)'}]
                    for entry in arrived:
                        content.append({'type': 'text', 'text': (
                            entry['content'] + '\n[Alex sent this while you were working. It continues or changes what '
                            'he asked, from now on: apply it before your next step.]')})
                    bounded = bound_result(content, agent.AGENT_DIR / 'context_archive' / 'results')
                    if isinstance(bounded, Failed):
                        return {'content': [{'type': 'text', 'text': str(bounded)}], 'isError': True}
                    return {'content': bounded, 'isError': isinstance(result, Failed)}
            return tool(schema['name'], schema.get('description', ''), schema['input_schema'])(handler)

        async def prompt():
            yield {'type': 'user', 'message': {'role': 'user', 'content': blocks}}

        opts = ClaudeAgentOptions(max_buffer_size=message_budget(), cli_path=cli, cwd=str(agent.RUNTIME_DIR),
            env={'ANTHROPIC_API_KEY': '', 'ANTHROPIC_AUTH_TOKEN': '',
                 'CLAUDE_CODE_USE_BEDROCK': '0', 'CLAUDE_CODE_USE_VERTEX': '0', 'CLAUDE_CODE_USE_FOUNDRY': '0'},
            system_prompt={'type': 'file', 'path': str(system_prompt_file)},
            tools=[], allowed_tools=[f"mcp__agent__{s['name']}" for s in schemas],
            mcp_servers={'agent': create_sdk_mcp_server('agent', '1.0.0', [make(s) for s in schemas])},
            permission_mode='bypassPermissions', model=model, max_turns=agent.MAX_TOOL_ITERATIONS,
            include_partial_messages=True,          # events during a long generation, so silence means a hung connection
            output_format=reply_envelope.CLAUDE_SDK_OUTPUT_FORMAT,   # the reply is a declared field (reply_envelope)
            setting_sources=[])
        final = None
        usage_recorded = False
        seen_usage = set()
        observed = {}
        usage_id = agent._inbox.record_event('response', model, 'subscription', 'claude-sdk', role='sdk_total', partial=True)
        input_fields = ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')

        def save_usage(usage, partial):
            nonlocal usage_id
            usage_id = agent._inbox.record_event('response', model, 'subscription', 'claude-sdk', usage,
                                                 role='sdk_total', event_id=usage_id, partial=partial)

        def record_step(event):
            if usage_recorded or event.parent_tool_use_id or not event.message_id or not isinstance(event.usage, dict):
                return
            if event.message_id in seen_usage:
                return
            seen_usage.add(event.message_id)
            for key in input_fields:
                value = event.usage.get(key)
                if type(value) is int and value >= 0:
                    observed[key] = observed.get(key, 0) + value
            # SDK per-step output_tokens is a placeholder. Only final result output is usable.
            save_usage(observed, partial=True)

        def record_usage(usage):
            nonlocal usage_recorded
            if usage_recorded or not isinstance(usage, dict):
                return
            # Crash results can be zeroed: never erase already reported input with a smaller summary.
            if any(type(usage.get(k)) is int and usage[k] < value for k, value in observed.items()):
                return
            valid = {k: usage[k] for k in input_fields + ('output_tokens',)
                     if type(usage.get(k)) is int and usage[k] >= 0}
            if not valid:
                return
            usage_recorded = True
            save_usage(observed | valid, partial=len(valid) < 4)

        start_lock = CliStartLock().acquire()
        # The stream is pumped into a queue so the consumer can notice silence. Partial messages keep events
        # flowing during a long generation; a hung connection produces nothing, and after SILENCE_SECONDS the
        # pump is cancelled (the SDK then closes the CLI) and the route steps aside.
        events = asyncio.Queue()
        DONE = object()

        async def pump():
            try:
                async for item in query(prompt=prompt(), options=opts):
                    # Persist before yielding to tool/wait handling; a killed worker retains its last snapshot.
                    if isinstance(item, AssistantMessage):
                        record_step(item)
                    elif isinstance(item, ResultMessage):
                        record_usage(item.usage)
                    await events.put(item)
            except BaseException as exc:          # includes CancelledError: the consumer decides what it means
                await events.put(exc)
            finally:
                await events.put(DONE)
        pump_task = asyncio.create_task(pump())
        try:
            while True:
                try:
                    event = await asyncio.wait_for(events.get(), timeout=30)
                except asyncio.TimeoutError:
                    if stream_silent(activity, time.time()):
                        pump_task.cancel()
                        raise Unavailable(f'Claude stream silent for {SILENCE_SECONDS // 60} minutes; connection presumed hung', 60)
                    continue
                if event is DONE:
                    break
                if isinstance(event, BaseException):
                    # SDK 0.2.163 can raise after yielding the structured terminal frame.
                    from claude_agent_sdk._errors import ResultError
                    if isinstance(event, ResultError) and event.subtype == 'error_max_turns':
                        record_usage(final.usage if final is not None else event.data.get('usage'))
                        if uncertain:
                            raise RuntimeError('Session limit with an unsettled tool; inspect receipts.') from event
                        raise SessionLimit('Claude max-turns session limit') from event
                    raise event
                activity['at'] = time.time()
                if start_lock.held and isinstance(event, (AssistantMessage, ResultMessage)):
                    start_lock.release()    # first model response: any token refresh is done
                agent._inbox.progress('model', deadline_seconds=2100)
                if isinstance(event, ResultMessage):
                    final = event
        finally:
            start_lock.release()
            if not pump_task.done():
                pump_task.cancel()
            try:
                await pump_task
            except asyncio.CancelledError:
                pass  # cancellation is acknowledged before a new session can start
            record_usage(final.usage if final is not None else None)
        if final is None:
            raise RuntimeError('Claude subscription ended without a completion record; no automatic replay.')
        if final.is_error:
            if uncertain:
                raise RuntimeError('Claude stopped with an uncertain tool outcome; inspect saved receipts.')
            if final.subtype == 'error_max_turns':
                raise SessionLimit('Claude max-turns session limit')
            status = getattr(final, 'api_error_status', None)
            detail = ' '.join(final.errors or []) + ' ' + (final.result or '')
            low = detail.lower()
            if status == 401 or any(x in low for x in AUTH_FAILURE_TEXT):
                raise Unavailable(SIGN_IN_REASON)
            if status == 429 or any(x in low for x in ('usage limit', 'rate limit', "you've hit your limit")):
                raise Unavailable('Claude subscription usage limit reached; retry in five hours', 5*3600, quota=True)
            if status in (500, 502, 503, 529):
                raise Unavailable('Claude service temporarily unavailable', 120)
            raise RuntimeError('Claude subscription did not finish: '+str(final.subtype))
        structured = getattr(final, 'structured_output', None)
        text = reply_envelope.reply(structured if structured is not None else (final.result or ''))
        if not agent.BACKGROUND_TASK_ID and not reminded and                 reply_envelope.unbacked(text, agent._background.activated_since(chat, turn_started)):
            next_retry.append(reply_envelope.retry_note(agent, 'unbacked_promise', text, reply_envelope.UNBACKED_PROMISE))
            return None                                     # one more session, with a wire-only reminder
        if not agent.BACKGROUND_TASK_ID and not reminded and reply_envelope.dead_end(text):
            next_retry.append(reply_envelope.retry_note(agent, 'dead_end', text, reply_envelope.DEAD_END))
            return None                                     # one more session: another route or a question
        missing = [] if agent.BACKGROUND_TASK_ID or reminded else reply_envelope.unanswered(
            text, await caller.call(agent._inbox.turn_messages) if hasattr(agent._inbox, 'turn_messages') else [])
        if missing:
            next_retry.append(reply_envelope.retry_note(agent, 'unanswered_message', text, reply_envelope.unanswered_note(missing)))
            return None                                     # one more session: cover every message Alex sent
        from agent_core.task_decisions import completion_feedback
        feedback = await caller.call(completion_feedback, agent, chat, text)
        if feedback:
            next_retry.append(feedback)
            return None
        messages.append({'role': 'assistant', 'content': text})
        # The shared delivery boundary verifies the draft before persisting it as a reply.
        return text

    try:
        result = _run_coroutine(run)
        if result is None:
            return run_claude(agent, messages, chat, model, reminded=True, retry=next_retry[0])
        return result
    except Exception as exc:
        from agent_core.session_lifecycle import TaskWaiting
        if isinstance(exc, TaskWaiting):
            raise
        # An expired/revoked login fails before any tool runs, so handing the turn to the next
        # route is safe. Previously "Failed to authenticate: OAuth session expired" fell through
        # as an unknown error and stopped the task (2026-10-06).
        if not uncertain and any(x in str(exc).lower() for x in AUTH_FAILURE_TEXT):
            raise Unavailable(SIGN_IN_REASON) from exc
        raise
