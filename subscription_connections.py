"""Subscription sign-in coordination. Persist status only, never OAuth URLs or tokens."""
from agent_core.openai_chatgpt import ChatGPTPlan
from agent_core.roster import AGENTS
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from urllib.parse import urlsplit
from agent_runtime import atomic_json
from subscription_runtime import find_cli, subscription_env, auth_status
ROOT = Path(__file__).resolve().parent
STATUS = ROOT/'dashboard/claude_login.json'


def report(status, message):
    atomic_json(STATUS, {'status':status,'message':message,'updated':time.time()})


def connect_claude():
    status=auth_status()
    if status.get('loggedIn') and status.get('authMethod')=='claude.ai':
        return True
    cli=find_cli()
    if not cli:
        raise RuntimeError('Claude Code is not installed.')
    report('waiting','Complete the Claude authorization page. This one sign-in serves all four agents.')
    proc=subprocess.Popen([cli,'auth','login','--claudeai'],env=subscription_env(),
        stdout=subprocess.PIPE,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW)
    opened=False
    def read_output():
        nonlocal opened
        for raw in iter(proc.stdout.readline,b''):
            line=re.sub(r'\x1b\[[0-9;]*[A-Za-z]','',raw.decode('utf-8','replace'))
            for url in re.findall(r'https://[^\s<>]+',line):
                host=(urlsplit(url).hostname or '').lower()
                if not opened and any(host==d or host.endswith('.'+d) for d in ('claude.ai','claude.com','anthropic.com')):
                    try:
                        os.startfile(url)
                        opened=True
                    except OSError:
                        report('waiting','Claude authorization could not open in the default browser. Check your default-browser setting.')
    reader=threading.Thread(target=read_output,daemon=True)
    reader.start()
    try:
        proc.wait(timeout=600)
    except subprocess.TimeoutExpired:
        proc.terminate();proc.wait(timeout=10)
        raise RuntimeError('Claude sign-in timed out. No connection was changed.')
    finally:
        reader.join(timeout=2)
        proc.stdout.close()
    status=auth_status()
    return bool(status.get('loggedIn') and status.get('authMethod')=='claude.ai')


def connect_all():
    for name in AGENTS:
        plan=ChatGPTPlan(name.capitalize())
        profile=plan.load() or {}
        if 'chatgpt.tokens.use.direct' not in profile.get('scopes',[]):
            report('waiting','Complete OpenAI authorization for '+name+'. Other existing connections are preserved.')
            _,enabled=plan.connect()
            if not enabled:
                raise RuntimeError(name+' did not grant ChatGPT plan usage.')
        atomic_json(ROOT/name/'task_state/route_openai_subscription.json',{})
    if not connect_claude():
        raise RuntimeError('Claude subscription sign-in did not complete.')
    for name in AGENTS:
        atomic_json(ROOT/name/'task_state/route_anthropic_subscription.json',{})
    report('connected','All four agents have OpenAI authorization and access to the shared Claude subscription sign-in.')
