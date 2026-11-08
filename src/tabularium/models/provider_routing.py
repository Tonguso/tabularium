"""Ordered primary-inference routes shared by all agents (no credentials here)."""
from contextvars import ContextVar
from pathlib import Path
import json
import re
import time
import uuid
from tabularium.core.agent_runtime import atomic_json

ACTIVE = ContextVar('primary_route', default=None)
KINDS = {'openai_subscription': 'ChatGPT subscription',
         'anthropic_subscription': 'Claude subscription',
         'openai_api': 'OpenAI API (paid)', 'anthropic_api': 'Anthropic API (paid)'}


class Unavailable(RuntimeError):
    def __init__(self, reason, retry_seconds=300, quota=False):
        super().__init__(reason)
        self.retry_seconds = retry_seconds
        self.quota = quota


def validate(value):
    if not isinstance(value, dict):
        raise ValueError('Provide a model sequence.')
    rows = value.get('routes')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 4:
        raise ValueError('Choose between one and four routes.')
    seen = set()
    clean = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Invalid route.')
        kind, model = row.get('kind'), row.get('model', '')
        if kind not in KINDS or kind in seen:
            raise ValueError('Each billing route can appear once.')
        if not isinstance(model, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]{1,99}', model):
            raise ValueError('Enter a valid model ID.')
        if not model.startswith('gpt-' if kind.startswith('openai') else 'claude-'):
            raise ValueError('The model must match the selected provider.')
        seen.add(kind)
        clean.append({'kind': kind, 'model': model})
    return {'enabled': True, 'routes': clean}


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {} if default is None else default


def config(root):
    value = read(Path(root)/'routing.json')
    if not value:
        raise ValueError("No model sequence configured. Save a sequence in the dashboard.")
    return validate(value)


def save(root, value):
    value = validate(value)
    value.update(revision=uuid.uuid4().hex, updated=time.time())
    atomic_json(Path(root)/'routing.json', value)
    return value


def label(route):
    return f"{route['model']} · {KINDS[route['kind']]}"


def provider():
    route = ACTIVE.get()
    if not route:
        return None
    return 'openai' if route['kind'].startswith('openai') else ('sdk' if route['kind'].endswith('subscription') else 'anthropic')


def model(family):
    route = ACTIVE.get()
    return route['model'] if route and route['kind'].startswith(family) else None


def failure(exc):
    """Only known rejected inference requests are safe to hand off automatically."""
    if isinstance(exc, Unavailable):
        return exc
    status = getattr(exc, 'status_code', None)
    if status == 401:
        return Unavailable('Sign-in or API key needs attention', 300)
    if status == 429:
        return Unavailable('Provider rate or usage limit reached', 300)
    if status in (500, 502, 503, 529):
        return Unavailable('Provider is temporarily unavailable', 120)
    # Do not turn policy, bad models, context errors, timeouts or unknown tool outcomes into billing switches.
    return None


def run(agent, messages, chat):
    settings = config(agent.AGENT_DIR)
    reasons = []
    agent._review_counts[chat] = 0
    agent._strip_old_images(messages, chat)
    state_path = agent.RUNTIME_DIR/'task_state'/'routing_state.json'
    for route in settings['routes']:
        cooldown_path = agent.AGENT_DIR/'task_state'/('route_'+route['kind']+'.json')
        cooldown = read(cooldown_path)
        if cooldown.get('model') == route['model'] and cooldown.get('retry_at', 0) > time.time():
            reasons.append(KINDS[route['kind']] + ': ' + cooldown.get('reason', 'cooldown'))
            continue
        token = ACTIVE.set(route)
        try:
            kind = route['kind']
            if kind == 'openai_api' and not agent.OPENAI_API_KEY:
                raise Unavailable('No OpenAI API key configured')
            if kind == 'anthropic_api' and not agent.ANTHROPIC_API_KEY:
                raise Unavailable('No Anthropic API key configured')
            atomic_json(state_path, {'route': route, 'status': 'attempting', 'updated': time.time()})
            if kind == 'anthropic_subscription':
                from tabularium.models.subscription_runtime import run_claude
                result = run_claude(agent, messages, chat, route['model'])
            elif kind == 'anthropic_api':
                result = agent.run_turn_anthropic(messages, chat)
            else:
                result = agent.run_turn_openai(messages, chat)
            atomic_json(state_path, {'route': route, 'status': 'completed', 'updated': time.time()})
            atomic_json(cooldown_path, {})
            return result
        except Exception as exc:
            rejected = failure(exc)
            if rejected is None:
                atomic_json(state_path, {'route': route, 'status': 'stopped', 'reason': 'Error requires attention; no automatic replay', 'updated': time.time()})
                raise
            reasons.append(KINDS[route['kind']] + ': ' + str(rejected))
            # A quota already announced and merely re-confirmed when its cooldown lapsed is not
            # news: Alex was told "limit reached" again five hours after the first notice
            # (2026-10-05). Announce only when a route newly runs out; success clears the record.
            subscription = route['kind'].endswith('subscription')
            # Needs a person: sign-in expired, or the Claude CLI is missing (2026-10-20: a silent two-day fallback).
            signed_out = subscription and any(x in str(rejected).lower() for x in ('sign-in', 'not installed'))
            already_announced = (bool(cooldown.get('notified') or cooldown.get('quota'))
                                 and cooldown.get('model') == route['model'])
            atomic_json(cooldown_path, {'model': route['model'], 'reason': str(rejected),
                                      'retry_at': time.time()+rejected.retry_seconds,
                                      'quota': bool(rejected.quota),
                                      'notified': bool(rejected.quota or signed_out)})
            atomic_json(state_path, {'route': route, 'status': 'unavailable', 'reason': str(rejected), 'updated': time.time()})
            if (rejected.quota or signed_out) and subscription and not already_announced:
                more = settings['routes'].index(route) < len(settings['routes']) - 1
                notice = (KINDS[route['kind']] + ' is unavailable: ' + str(rejected) if 'not installed' in str(rejected).lower()
                          else KINDS[route['kind']] + ' sign-in has expired and needs reconnecting.' if signed_out
                          else KINDS[route['kind']] + ' usage limit reached.')
                notice += ' Continuing through your configured fallback sequence.' if more else ' No further fallback is configured.'
                try:
                    agent.send_message(chat, notice)
                except Exception:
                    print('[routing] Quota notice could not be delivered.')
        finally:
            ACTIVE.reset(token)
    raise RuntimeError('No configured route is available. ' + '; '.join(reasons) + '. Completed tool records are preserved.')
