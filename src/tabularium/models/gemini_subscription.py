"""Gemini subscription adapter via Antigravity CLI (agy) and loopback Streamable HTTP MCP."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import subprocess
import threading
import time
import uuid

from tabularium.paths import home as _home
from tabularium.core.context_window import archive_preview
from tabularium.core.outcome import Failed
from tabularium.core.session_lifecycle import SessionLimit
from tabularium.core.wire_budget import (
    CLI_RESULT_COPIES, ENVELOPE_HEADROOM, MAX_IMAGES, bound_result, json_size, message_budget,
)
from tabularium.models.provider_routing import Unavailable

START_SECONDS = 60          # no stream event at all for this long after launch: the route never started (2026-11-15 16:16)
GEMINI_SIGN_IN_REASON = 'Gemini subscription sign-in expired'
GEMINI_AUTH_FAILURE_TEXT = (
    'authentication required', 'not logged in', 'not authenticated', 'login required',
    'unauthenticated', 'invalid_grant', 'token has been expired or revoked', 're-authenticate',
)


def find_gemini_cli():
    local = os.environ.get('LOCALAPPDATA', '')
    candidates = [
        shutil.which('agy.exe'),
        shutil.which('agy'),
        str(Path(local) / 'agy/bin/agy.exe') if local else '',
        str(Path.home() / '.local/bin/agy.exe'),
    ]
    candidates = [p for p in candidates if p and Path(p).is_file()]
    return max(candidates, key=os.path.getmtime) if candidates else None


def gemini_subscription_env():
    # Strip API/Vertex variables so agy never bills a key instead of the Google subscription.
    return dict(os.environ, GEMINI_API_KEY='', GOOGLE_API_KEY='', GOOGLE_GENAI_USE_VERTEXAI='')


def gemini_auth_status():
    cli = find_gemini_cli()
    if not cli:
        return {'loggedIn': False, 'message': 'Antigravity CLI is not installed'}
    with GeminiStartLock():
        result = subprocess.run(
            [cli, '-p', '/usage', '--output-format', 'json'],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=25, env=gemini_subscription_env(),
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
    try:
        payload = json.loads(result.stdout or '{}')
    except ValueError:
        payload = {}
    if result.returncode == 0 and payload.get('status') == 'SUCCESS':
        return {'loggedIn': True, 'authMethod': 'google'}
    return {'loggedIn': False, 'message': GEMINI_SIGN_IN_REASON}


class GeminiStartLock:
    """Machine-wide lock held while an agy CLI process starts and refreshes OAuth tokens."""

    PATH = _home() / 'logs' / 'gemini_cli_start.lock'

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
                if time.time() > deadline:
                    self.handle.close()
                    self.handle = None
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


def cleanup_agy_conversation(conversation_id):
    """Remove agy's transient headless transcript folder so agent histories stay inside the agent home."""
    if isinstance(conversation_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{8,80}', conversation_id):
        brain = Path.home() / '.gemini' / 'antigravity-cli' / 'brain' / conversation_id
        if brain.exists():
            shutil.rmtree(brain, ignore_errors=True)


def _write_session_files(session_dir, system_prompt, port=None, secret='', tools=('finish',), enable_mcp=True, schema=None):
    (session_dir / '.git').mkdir(parents=True, exist_ok=True)      # stop agy walking up into repo rules
    agents_root = session_dir / '.agents'
    agents_root.mkdir(parents=True, exist_ok=True)
    if enable_mcp and port:
        (agents_root / 'mcp_config.json').write_text(json.dumps({
            'mcpServers': {
                'agent': {
                    'serverUrl': f'http://127.0.0.1:{port}/mcp',
                    'headers': {'Authorization': f'Bearer {secret}'},
                    'timeoutSeconds': 600,
                }
            }
        }), encoding='utf-8')
    else:
        (agents_root / 'mcp_config.json').write_text(json.dumps({'mcpServers': {}}), encoding='utf-8')
    agent_dir = agents_root / 'agents' / 'tabularium'
    agent_dir.mkdir(parents=True, exist_ok=True)
    tool_lines = '\n'.join(f'  - {t}' for t in tools) if tools else ' []'
    frontmatter = (
        '---\n'
        'name: tabularium\n'
        'description: Tabularium session agent\n'
        f'enableMcpTools: {"true" if enable_mcp else "false"}\n'
        'commandExecutionPolicy: "off"\n'
        f'tools:{"" if tools else tool_lines}\n'
        + (tool_lines + '\n' if tools else '')
        + '---\n'
    )
    (agent_dir / 'agent.md').write_text(frontmatter + system_prompt, encoding='utf-8')
    settings_file = session_dir / 'settings.json'
    settings_file.write_text(json.dumps({
        'modelProvider': '',
        'notifications': False,
        'showTips': False,
        'showFeedbackSurvey': False,
        'historySize': 0,
    }), encoding='utf-8')
    schema_file = None
    if schema is not None:
        schema_file = session_dir / 'schema.json'
        schema_file.write_text(json.dumps(schema), encoding='utf-8')
    return settings_file, schema_file


class _LoopbackMCPServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True


def _classify_gemini_failure(detail):
    low = (detail or '').lower()
    if any(x in low for x in GEMINI_AUTH_FAILURE_TEXT) or 'status 401' in low or 'code 16' in low:
        return Unavailable(GEMINI_SIGN_IN_REASON)
    if any(x in low for x in ('resource_exhausted', 'quota', 'usage limit', 'rate limit', '429', 'limit reached')):
        return Unavailable('Gemini subscription usage limit reached; retry in five hours', 5 * 3600, quota=True)
    if any(x in low for x in ('500', '502', '503', '529', 'temporarily unavailable', 'overloaded', 'service unavailable')):
        return Unavailable('Gemini service temporarily unavailable', 120)
    return None


def run_gemini(agent, messages, chat, model, reminded=False, retry=None):
    from tabularium.core import reply_envelope
    from tabularium.core.session_lifecycle import TaskWaiting
    from tabularium.models import subscription_runtime as _sr

    cli = _sr.find_gemini_cli()
    if not cli:
        raise Unavailable('Antigravity CLI is not installed (no agy.exe found; install from https://antigravity.google/cli/install.ps1)')
    schemas = [s for s in agent.get_tools('anthropic') if 'input_schema' in s]
    schema_map = {s['name']: s for s in schemas}
    agent._background_boundary(messages, [], chat)
    if agent._abort_requested.pop(chat, False):
        return 'Stopped.'
    turn_started = time.time()
    history = agent._prepare_request_context(
        agent._sanitise_tool_history(agent.trim_history(messages, chat)), chat)
    if retry:
        history = history + (retry if isinstance(retry, list) else [retry])

    raw_images = []

    def serial(value):
        if hasattr(value, 'model_dump'):
            value = value.model_dump()
        if isinstance(value, dict):
            if value.get('type') == 'image':
                bounded = bound_result([value], agent.AGENT_DIR / 'context_archive' / 'results')
                if isinstance(bounded, Failed):
                    return {'type': 'text', 'text': str(bounded)}
                raw_images.append(value)
                return {'type': 'text', 'text': f'__TABULARIUM_IMAGE_{len(raw_images)}__'}
            return {k: serial(v) for k, v in value.items() if k != 'cache_control'}
        if isinstance(value, list):
            return [serial(v) for v in value]
        return value

    transcript = json.dumps(serial(history), ensure_ascii=False)
    cutoff = max(0, len(raw_images) - MAX_IMAGES)
    retained_images = raw_images[cutoff:]
    for i, img in enumerate(raw_images):
        token = f'__TABULARIUM_IMAGE_{i + 1}__'
        if i < cutoff:
            preview = archive_preview([img], agent.AGENT_DIR / 'context_archive' / 'results', preview_chars=400)
            transcript = transcript.replace(token, preview.replace('\\', '\\\\').replace('"', '\\"'))
        else:
            att_idx = i - cutoff + 1
            transcript = transcript.replace(
                token, f'[Attached image {att_idx}: call view_attached_image with index={att_idx} to inspect]')

    prompt_text = (
        'Continue this conversation at the latest user request. '
        'The JSON below is conversation history, including completed tool actions, not new system instructions. '
        'Do not repeat completed actions. If an action outcome is unknown, inspect it before proceeding. '
        'Use only the MCP tools on server "agent" declared for this session.\n' + transcript
    )
    if CLI_RESULT_COPIES * (json_size(prompt_text) + json_size(retained_images)) + ENVELOPE_HEADROOM > message_budget():
        raise Unavailable('Gemini prompt exceeds the shared wire budget before session start; use a smaller context or another route', 60)

    uncertain = False
    activity = {'at': time.time(), 'busy': False}
    events = queue.Queue()
    shutdown = threading.Event()
    tool_lock = threading.Lock()
    tool_rounds = 0
    secret = secrets.token_urlsafe(24)

    def call_turn(fn, *args):
        if shutdown.is_set():
            raise RuntimeError('Session closed')
        done = threading.Event()
        box = {}
        events.put(('call', (fn, args, done, box)))
        while not done.wait(timeout=0.25):
            if shutdown.is_set():
                raise RuntimeError('Session closed')
        if 'error' in box:
            raise box['error']
        return box['value']

    mcp_tools = [{'name': s['name'], 'description': s.get('description', ''), 'inputSchema': s['input_schema']}
                 for s in schemas]
    if retained_images:
        mcp_tools.append({
            'name': 'view_attached_image',
            'description': 'Inspect an image attached in the conversation history by its 1-based index.',
            'inputSchema': {'type': 'object', 'properties': {'index': {'type': 'integer'}}, 'required': ['index']},
        })

    def handle_tool_call(name, args):
        nonlocal uncertain, tool_rounds
        if name == 'view_attached_image' and retained_images:
            idx = int((args or {}).get('index', 0))
            if not 1 <= idx <= len(retained_images):
                return {'content': [{'type': 'text', 'text': f'Invalid image index {idx}.'}], 'isError': True}
            src = retained_images[idx - 1].get('source') or {}
            return {'content': [{'type': 'image', 'data': src.get('data', ''), 'mimeType': src.get('media_type', 'image/png')}],
                    'isError': False}
        schema = schema_map.get(name)
        if schema is None:
            return {'content': [{'type': 'text', 'text': f'Unknown tool: {name}'}], 'isError': True}
        with tool_lock:
            tool_rounds += 1
            if tool_rounds > agent.MAX_TOOL_ITERATIONS:
                events.put(('exception', SessionLimit('Gemini max-turns session limit')))
                return {'content': [{'type': 'text', 'text': 'Session tool-round limit reached.'}], 'isError': True}
            try:
                arrived = call_turn(agent._background_boundary, messages, [], chat) or []
            except TaskWaiting as exc:
                events.put(('exception', exc))
                return {'content': [{'type': 'text', 'text': 'Not executed: waiting for the recorded decision.'}], 'isError': True}
            if agent._abort_requested.pop(chat, False):
                events.put(('exception', RuntimeError('Stopped by user')))
                return {'content': [{'type': 'text', 'text': 'Stopped by user.'}], 'isError': True}
            ident = 'route_' + uuid.uuid4().hex
            result_block = {
                'type': 'tool_result', 'tool_use_id': ident,
                'content': 'Outcome unknown: tool started; inspect action receipts before retrying.', 'is_error': True,
            }
            messages.extend([
                {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': ident, 'name': name, 'input': dict(args or {})}]},
                {'role': 'user', 'content': [result_block]},
            ])
            call_turn(agent.save_conversation, chat)
            uncertain = True
            activity['busy'] = True
            try:
                result = call_turn(agent.execute_tool, name, dict(args or {}), chat)
            finally:
                activity['busy'] = False
                activity['at'] = time.time()
            result_block['content'] = result if isinstance(result, (str, list)) else str(result)
            if isinstance(result, Failed):
                result_block['is_error'] = True
            else:
                result_block.pop('is_error', None)
            call_turn(agent.save_conversation, chat)
            uncertain = False
            try:
                if agent.BACKGROUND_TASK_ID:
                    call_turn(agent._check_task_waiting, chat)
            except TaskWaiting as exc:
                events.put(('exception', exc))
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

    class MCPHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(404 if self.path.startswith('/.well-known/') else 405)
            self.end_headers()

        def do_POST(self):
            if self.headers.get('Authorization') != f'Bearer {secret}':
                self.send_response(403)
                self.end_headers()
                return
            size = int(self.headers.get('Content-Length', '0'))
            req = json.loads(self.rfile.read(size))
            mid = req.get('id')
            method = req.get('method')
            params = req.get('params') or {}
            if mid is None:
                self.send_response(202)
                self.end_headers()
                return
            payload = {'jsonrpc': '2.0', 'id': mid}
            try:
                if method == 'initialize':
                    payload['result'] = {
                        'protocolVersion': params.get('protocolVersion', '2025-03-26'),
                        'capabilities': {'tools': {}},
                        'serverInfo': {'name': 'agent', 'version': '1.0.0'},
                    }
                elif method == 'tools/list':
                    payload['result'] = {'tools': mcp_tools}
                elif method == 'tools/call':
                    payload['result'] = handle_tool_call(params.get('name', ''), params.get('arguments') or {})
                else:
                    payload['error'] = {'code': -32601, 'message': 'Method not found'}
            except Exception as exc:
                payload['error'] = {'code': -32000, 'message': str(exc)[:300]}
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = _LoopbackMCPServer(('127.0.0.1', 0), MCPHandler)
    threading.Thread(target=server.serve_forever, daemon=True, name='gemini-mcp-bridge').start()
    session_dir = Path(agent.RUNTIME_DIR) / 'task_state' / 'agy_session'
    settings_file, schema_file = _write_session_files(
        session_dir,
        agent.SYSTEM_PROMPT + f'\n[Active model: {model}, Gemini subscription.]',
        port=server.server_port,
        secret=secret,
        tools=('finish',),
        enable_mcp=True,
        schema=reply_envelope.SCHEMA,
    )

    usage_id = agent._inbox.record_event('response', model, 'subscription', 'gemini-cli', role='sdk_total', partial=True)
    usage_recorded = False
    seen_steps = set()
    observed = {}
    input_fields = ('input_tokens', 'cache_read_tokens')

    def save_usage(u, partial):
        nonlocal usage_id
        usage_id = agent._inbox.record_event('response', model, 'subscription', 'gemini-cli', u,
                                             role='sdk_total', event_id=usage_id, partial=partial)

    def record_step(step):
        if usage_recorded or step.get('state') != 'DONE' or not isinstance(step.get('usage'), dict):
            return
        idx = step.get('step_index')
        if idx is not None:
            if idx in seen_steps:
                return
            seen_steps.add(idx)
        for key in input_fields:
            val = step['usage'].get(key)
            if type(val) is int and val >= 0:
                observed[key] = observed.get(key, 0) + val
        if observed:
            save_usage(observed, partial=True)

    def record_usage(u):
        nonlocal usage_recorded
        if usage_recorded or not isinstance(u, dict):
            return
        if any(type(u.get(k)) is int and u[k] < val for k, val in observed.items()):
            return
        valid = {k: u[k] for k in input_fields + ('output_tokens',) if type(u.get(k)) is int and u[k] >= 0}
        if not valid:
            return
        usage_recorded = True
        save_usage(observed | valid, partial='output_tokens' not in valid or 'input_tokens' not in valid)

    start_lock = GeminiStartLock().acquire()
    proc = None
    conversation_id = None
    final = None
    raised = None
    stderr_text = ''
    try:
        proc = subprocess.Popen(
            [cli, '--input-format', 'stream-json', '--output-format', 'stream-json',
             '--model', model, '--agent', 'tabularium', '--json-schema', str(schema_file),
             '--settings', str(settings_file), '--disable-slash-commands', '--dangerously-skip-permissions'],
            cwd=str(session_dir), env=gemini_subscription_env(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', errors='replace',
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        proc.stdin.write(json.dumps({'event': 'user', 'message': {'content': prompt_text}}, ensure_ascii=False) + '\n')
        proc.stdin.flush()
        proc.stdin.close()

        def pump_stdout():
            try:
                for raw in iter(proc.stdout.readline, ''):
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        events.put(('stream', json.loads(line)))
                    except ValueError:
                        continue
            except BaseException as exc:
                events.put(('exception', exc))
            finally:
                events.put(('eof', None))

        def pump_stderr():
            nonlocal stderr_text
            try:
                stderr_text = proc.stderr.read() or ''
            except Exception:
                pass

        threading.Thread(target=pump_stdout, daemon=True, name='gemini-stdout').start()
        stderr_thread = threading.Thread(target=pump_stderr, daemon=True, name='gemini-stderr')
        stderr_thread.start()

        launched, started = time.time(), False
        while True:
            try:
                kind, payload = events.get(timeout=1.0)
            except queue.Empty:
                if not started and time.time() - launched > START_SECONDS:
                    # 2026-11-15 16:16: the CLI opened a conversation and produced no step for ten minutes; the turn
                    # waited out the silence limit, the supervisor killed the agent and the owner's message was lost.
                    raise Unavailable(f'Gemini produced no event within {START_SECONDS} seconds of launch; route skipped', 60)
                if _sr.stream_silent(activity, time.time()):
                    raise Unavailable(f'Gemini stream silent for {_sr.SILENCE_SECONDS // 60} minutes; connection presumed hung', 60)
                continue
            started = True
            if kind == 'call':
                fn, args, done, box = payload
                try:
                    box['value'] = fn(*args)
                except BaseException as exc:
                    box['error'] = exc
                finally:
                    done.set()
                continue
            if kind == 'eof':
                break
            if kind == 'exception':
                raised = payload
                break
            if kind == 'stream' and isinstance(payload, dict):
                activity['at'] = time.time()
                ev_type = payload.get('event')
                if ev_type == 'init':
                    conversation_id = payload.get('conversation_id') or conversation_id
                elif ev_type == 'step_update':
                    if start_lock.held:
                        start_lock.release()
                    step = payload.get('step_update') or {}
                    conversation_id = step.get('conversation_id') or conversation_id
                    record_step(step)
                elif ev_type == 'result':
                    if start_lock.held:
                        start_lock.release()
                    final = payload.get('result') or {}
                    conversation_id = final.get('conversation_id') or conversation_id
                    record_usage(final.get('usage'))
                agent._inbox.progress('model', deadline_seconds=2100)
        stderr_thread.join(timeout=2)
    finally:
        shutdown.set()
        start_lock.release()
        while True:
            try:
                kind, payload = events.get_nowait()
            except queue.Empty:
                break
            if kind == 'call':
                _, _, done, box = payload
                box['error'] = RuntimeError('Session closed')
                done.set()
        if proc is not None:
            if proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            else:
                try:
                    proc.wait(timeout=2)
                except Exception:
                    pass
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except Exception:
                    pass
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass
        record_usage(final.get('usage') if isinstance(final, dict) else None)
        cleanup_agy_conversation(conversation_id)

    if raised is not None:
        if isinstance(raised, SessionLimit) and uncertain:
            raise RuntimeError('Session limit with an unsettled tool; inspect receipts.') from raised
        raise raised
    if final is None:
        if not uncertain:
            classified = _classify_gemini_failure(stderr_text)
            if classified is not None:
                raise classified
        raise RuntimeError('Gemini subscription ended without a completion record; no automatic replay.')
    if final.get('status') != 'SUCCESS':
        if uncertain:
            raise RuntimeError('Gemini stopped with an uncertain tool outcome; inspect saved receipts.')
        detail = (final.get('error') or '') + ' ' + stderr_text
        classified = _classify_gemini_failure(detail)
        if classified is not None:
            raise classified
        raise RuntimeError('Gemini subscription did not finish: ' + (final.get('error') or str(final.get('status')))[:240])

    structured = final.get('structured_output')
    raw_resp = final.get('response') or ''
    try:
        text = reply_envelope.reply(structured if structured is not None else raw_resp)
    except reply_envelope.ReplyMissing:
        if reminded:
            raise
        note = reply_envelope.retry_note(agent, 'reply_missing', raw_resp, reply_envelope.REPLY_MISSING)
        return run_gemini(agent, messages, chat, model, reminded=True, retry=note)
    if not agent.BACKGROUND_TASK_ID and not reminded and reply_envelope.unbacked(text, agent._background.activated_since(chat, turn_started)):
        note = reply_envelope.retry_note(agent, 'unbacked_promise', text, reply_envelope.UNBACKED_PROMISE)
        return run_gemini(agent, messages, chat, model, reminded=True, retry=note)
    if not agent.BACKGROUND_TASK_ID and not reminded and reply_envelope.dead_end(text):
        note = reply_envelope.retry_note(agent, 'dead_end', text, reply_envelope.DEAD_END)
        return run_gemini(agent, messages, chat, model, reminded=True, retry=note)
    turn_msgs = agent._inbox.turn_messages() if hasattr(agent._inbox, 'turn_messages') else []
    missing = [] if agent.BACKGROUND_TASK_ID or reminded else reply_envelope.unanswered(text, turn_msgs)
    if missing:
        note = reply_envelope.retry_note(agent, 'unanswered_message', text, reply_envelope.unanswered_note(missing))
        return run_gemini(agent, messages, chat, model, reminded=True, retry=note)
    undecided = [] if agent.BACKGROUND_TASK_ID or reminded else reply_envelope.unconsidered(text, turn_msgs)
    if undecided:
        note = reply_envelope.retry_note(agent, 'strong_language_undecided', text, reply_envelope.unconsidered_note(undecided))
        return run_gemini(agent, messages, chat, model, reminded=True, retry=note)
    from tabularium.records.task_decisions import DECISION_NOTE, completion_feedback, declaration_error
    error = None if agent.BACKGROUND_TASK_ID or reminded or not (getattr(text, 'question', '') or getattr(text, 'decision_id', 0)) else \
        declaration_error(agent._background, chat, text, int(getattr(text, 'decision_task_id', 0)))
    if error:
        note = reply_envelope.retry_note(agent, 'decision_mismatch', text, DECISION_NOTE.format(error=error))
        return run_gemini(agent, messages, chat, model, reminded=True, retry=note)
    feedback = completion_feedback(agent, chat, text)
    if feedback:
        prior = retry if isinstance(retry, list) else ([retry] if retry else [])
        return run_gemini(agent, messages, chat, model, reminded=True, retry=prior + [feedback])
    messages.append({'role': 'assistant', 'content': text})
    return text
