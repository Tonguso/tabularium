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
         'gemini_subscription': 'Gemini subscription',
         'openai_api': 'OpenAI API (paid)', 'anthropic_api': 'Anthropic API (paid)',
         'gemini_api': 'Gemini API (paid)'}


class Unavailable(RuntimeError):
    def __init__(self, reason, retry_seconds=300, quota=False):
        super().__init__(reason)
        self.retry_seconds = retry_seconds
        self.quota = quota


def validate(value):
    if not isinstance(value, dict):
        raise ValueError('Provide a model sequence.')
    rows = value.get('routes')
    if not isinstance(rows, list) or not 1 <= len(rows) <= len(KINDS):
        raise ValueError(f'Choose between one and {len(KINDS)} routes.')
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
        prefix = 'gpt-' if kind.startswith('openai') else ('gemini-' if kind.startswith('gemini') else 'claude-')
        if not model.startswith(prefix):
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
        raise ValueError("No model sequence configured: routing.json is missing from the agent folder.")
    return validate(value)


def save(root, value):
    value = validate(value)
    value.update(revision=uuid.uuid4().hex, updated=time.time())
    atomic_json(Path(root)/'routing.json', value)
    return value


def label(route):
    return f"{route['model']} · {KINDS[route['kind']]}"


# A temporary switch to one route, beside the permanent order: Alex asked to switch to OpenAI for a
# while, not to reorder routing.json (2026-11-14). Its own file, so routing.json is never rewritten.
OVERRIDE = 'routing_override.json'


def _checked_override(kind, model, reason, expires, now):
    route = validate({'routes': [{'kind': kind, 'model': model}]})['routes'][0]
    if not isinstance(reason, str):
        raise ValueError('The reason must be text.')
    if expires is not None and (isinstance(expires, bool) or not isinstance(expires, (int, float)) or expires <= now):
        raise ValueError('The expiry must be a future time, or none until cleared.')
    return route


def override(root, now=None):
    """The valid, unexpired override, or None. Read inside every turn, so it never raises."""
    try:
        value = read(Path(root)/OVERRIDE)
        if not value:
            return None
        now = time.time() if now is None else now
        route = _checked_override(value.get('kind'), value.get('model'), value.get('reason', ''),
                                  value.get('expires'), now)
        return dict(route, reason=value.get('reason', ''), set=value.get('set'), expires=value.get('expires'))
    except Exception:
        return None


def set_override(root, kind, model, reason, expires=None):
    now = time.time()
    route = _checked_override(kind, model, reason, expires, now)
    value = dict(route, reason=reason, set=now, expires=expires)
    atomic_json(Path(root)/OVERRIDE, value)
    return value


def clear_override(root):
    (Path(root)/OVERRIDE).unlink(missing_ok=True)


def effective_routes(root):
    """The override first, then the permanent sequence without its billing kind, in saved order."""
    routes = config(root)['routes']
    current = override(root)
    if not current:
        return routes
    return [{'kind': current['kind'], 'model': current['model']}] + [r for r in routes if r['kind'] != current['kind']]


def cooling(record, route):
    """True while a route's cooldown record still holds for the model it names."""
    return record.get('model') == route['model'] and record.get('retry_at', 0) > time.time()


def first_open(root):
    """The route the next turn tries first: the first of the sequence that is not cooling down. None when all are."""
    for route in effective_routes(root):
        if not cooling(read(Path(root)/'task_state'/('route_'+route['kind']+'.json')), route):
            return route
    return None


def override_label(current):
    until = time.strftime('until %Y-%m-%d %H:%M', time.localtime(current['expires'])) if current.get('expires') else 'until cleared'
    return f"Temporary override: {label(current)} ({until})"


def provider():
    route = ACTIVE.get()
    if not route:
        return None
    if route['kind'] == 'gemini_api':
        return 'gemini'
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
    # Do not turn policy, bad models, context errors or unknown tool outcomes into billing switches.
    return None


def timed_out_before_acting(exc, agent):
    """A provider timeout that cannot have had an effect, so the next route may answer it.

    Timeouts stay off the hand-off list above because a request that timed out may still have run and
    executed tools, and repeating those on another route is not safe. When this turn has executed no tool
    there is nothing to repeat (2026-11-19: Alex's 20:40 questions died on a Claude Code timeout at step
    zero and the whole turn stopped, though three further routes were configured)."""
    if not any(word in str(exc).lower() for word in ('timed out', 'timeout')):
        return None
    if getattr(getattr(agent, '_inbox', None), 'local', None) is None:
        return None
    if getattr(agent._inbox.local, 'tool_ids', None):
        return None
    return Unavailable('Provider timed out before any tool ran', 60)


def run(agent, messages, chat):
    routes = effective_routes(agent.AGENT_DIR)
    reasons = []
    agent._review_counts[chat] = 0
    agent._strip_old_images(messages, chat)
    state_path = agent.RUNTIME_DIR/'task_state'/'routing_state.json'
    for route in routes:
        cooldown_path = agent.AGENT_DIR/'task_state'/('route_'+route['kind']+'.json')
        cooldown = read(cooldown_path)
        if cooling(cooldown, route):
            reasons.append(KINDS[route['kind']] + ': ' + cooldown.get('reason', 'cooldown'))
            continue
        token = ACTIVE.set(route)
        try:
            kind = route['kind']
            if kind == 'openai_api' and not agent.OPENAI_API_KEY:
                raise Unavailable('No OpenAI API key configured')
            if kind == 'anthropic_api' and not agent.ANTHROPIC_API_KEY:
                raise Unavailable('No Anthropic API key configured')
            if kind == 'gemini_api' and not getattr(agent, 'GEMINI_API_KEY', ''):
                raise Unavailable('No Gemini API key configured')
            atomic_json(state_path, {'route': route, 'status': 'attempting', 'updated': time.time()})
            if kind == 'anthropic_subscription':
                from tabularium.models.subscription_runtime import run_claude
                result = run_claude(agent, messages, chat, route['model'])
            elif kind == 'gemini_subscription':
                from tabularium.models.subscription_runtime import run_gemini
                result = run_gemini(agent, messages, chat, route['model'])
            elif kind == 'anthropic_api':
                result = agent.run_turn_anthropic(messages, chat)
            elif kind == 'gemini_api':
                result = agent.run_turn_gemini(messages, chat)
            else:
                result = agent.run_turn_openai(messages, chat)
            atomic_json(state_path, {'route': route, 'status': 'completed', 'updated': time.time()})
            atomic_json(cooldown_path, {})
            return result
        except Exception as exc:
            rejected = failure(exc) or timed_out_before_acting(exc, agent)
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
                more = routes.index(route) < len(routes) - 1
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
