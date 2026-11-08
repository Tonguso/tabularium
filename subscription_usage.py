"""Cached allowance reads; no inference prompts and no credentials in cached/UI data."""
from agent_core.openai_chatgpt import ChatGPTPlan
from agent_core.roster import AGENTS
from agent_core.wire_budget import message_budget
import asyncio
import json
import subprocess
from pathlib import Path
import threading
import time
from agent_runtime import atomic_json
from subscription_runtime import find_cli, subscription_env
ROOT=Path(__file__).resolve().parent
CACHE=ROOT/'dashboard/subscription_usage.json'
LOCK=threading.Lock()
LAST_ATTEMPT=0


def read_cache():
    try: return json.loads(CACHE.read_text(encoding='utf-8'))
    except (OSError,ValueError): return {}


def openai_status():
    rows=[]
    for name in AGENTS:
        try:
            plan=ChatGPTPlan(name.capitalize())
            profile=plan.load() or {}
            rows.append({'agent':name,'authorized':'chatgpt.tokens.use.direct' in profile.get('scopes',[]),
                         'limit_reported':bool(profile.get('quota_exhausted')),
                         'retry_at':profile.get('quota_retry_at')})
        except Exception:
            rows.append({'agent':name,'authorized':None,'limit_reported':None})
    return {'agents':rows,'updated':time.time(),'available':False,
            'note':'Remaining allowance is not exposed by the agents’ sign-in integration. View OpenAI Usage for account and app limits.'}


async def claude_usage():
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
    cli=find_cli()
    if not cli: raise RuntimeError('Claude CLI unavailable')
    options=ClaudeAgentOptions(max_buffer_size=message_budget(), cli_path=cli,env=subscription_env(),cwd=str(ROOT/'dashboard'),
                              tools=[],mcp_servers={},setting_sources=[])
    # This refresh owns its asyncio.run loop. Hide both SDK version checks and
    # the CLI itself without patching the SDK or other dashboard threads.
    loop=asyncio.get_running_loop()
    start_process=loop.subprocess_exec
    async def hidden_process(*args, **kwargs):
        kwargs['creationflags']=kwargs.get('creationflags',0) | getattr(subprocess,'CREATE_NO_WINDOW',0)
        return await start_process(*args,**kwargs)
    loop.subprocess_exec=hidden_process
    from subscription_runtime import CliStartLock
    try:
        # Same machine-wide lock as the agents: this CLI shares their single-use refresh token.
        with CliStartLock():
            async with ClaudeSDKClient(options=options) as client:
                # Installed Python SDK has no public wrapper yet; isolate this CLI control call here.
                data=await client._query._send_control_request({'subtype':'get_usage','skip_behaviors':True})
    finally:
        loop.subprocess_exec=start_process
    limits=data.get('rate_limits') or {}
    windows=[]
    names={'five_hour':'Five-hour window','seven_day':'Weekly allowance','seven_day_opus':'Weekly · Opus',
           'seven_day_sonnet':'Weekly · Sonnet','seven_day_oauth_apps':'Weekly · connected apps'}
    for key,title in names.items():
        value=limits.get(key)
        if not isinstance(value,dict): continue
        used=value.get('utilization')
        if not isinstance(used,(int,float)): used=None
        windows.append({'name':title,'used':used,'remaining':None if used is None else max(0,min(100,100-used)),
                        'resets_at':value.get('resets_at')})
    spend=limits.get('spend') or {}
    amount=spend.get('used') or {}
    extra=None
    if spend.get('enabled') and isinstance(amount.get('amount_minor'),(int,float)):
        extra={'amount':amount['amount_minor']/10**amount.get('exponent',2),'currency':amount.get('currency')}
    return {'available':bool(data.get('rate_limits_available')),'plan':data.get('subscription_type'),
            'windows':windows,'extra_usage':extra,'updated':time.time()}


def refresh():
    previous=read_cache()
    result=dict(previous,checked=time.time(),error=None)
    try:
        result['openai']=openai_status()
        result['claude']=asyncio.run(asyncio.wait_for(claude_usage(),45))
    except Exception as exc:
        # Preserve last successful measurements, visibly stale; do not publish provider error bodies.
        result['error']='Allowance refresh unavailable ('+type(exc).__name__+'). Last successful figures retained.'
    finally:
        try: atomic_json(CACHE,result)
        finally: LOCK.release()


def snapshot(force=False):
    global LAST_ATTEMPT
    data=read_cache()
    if force and time.time()-max(LAST_ATTEMPT,data.get('checked',0))>=30 and LOCK.acquire(blocking=False):
        LAST_ATTEMPT=time.time()
        threading.Thread(target=refresh,daemon=True,name='subscription-usage').start()
    data['refreshing']=LOCK.locked()
    return data
