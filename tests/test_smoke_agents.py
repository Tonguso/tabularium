"""Offline: each agent builds in a fresh interpreter from its role module (fake credentials, temp
agent dir, no polling) and the shared functions are bound to the agent's namespace. start_all runs this
before stopping anything, so an agent that cannot import never replaces a running one.
2026-10-07: the first merged engine imported a Cato-only module and three agents died at startup."""
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ("coordinator", "private", "librarian", "engineer")

CHILD = r'''
import os, sys, tempfile, json
from pathlib import Path
ROOT, a = Path(sys.argv[1]), sys.argv[2]
agent_dir = ROOT / a
tmp = Path(tempfile.mkdtemp(prefix=f"smoke_{a}_")).resolve()      # the runtime resolves its home; compare like with like
(tmp / "SKILL.md").write_text("Test identity", encoding="utf-8")
(tmp / "memory.md").write_text("Test core\n<!-- MEMORY-DETAIL-BELOW -->\ndetail", encoding="utf-8")
style = Path("skills/talk-like-a-human-being/runtime-core.md")
if (agent_dir / style).exists():
    (tmp / style).parent.mkdir(parents=True, exist_ok=True)
    (tmp / style).write_bytes((agent_dir / style).read_bytes())
(tmp / "task_state" / "background" / "1").mkdir(parents=True)
for k in [k for k in os.environ if "TELEGRAM_BOT_TOKEN" in k]:
    del os.environ[k]
os.environ.update(ANTHROPIC_API_KEY="test", SEARCH_PROVIDER="anthropic", OPENAI_API_KEY="test")
os.environ[f"{a.upper()}_BACKGROUND_TASK"] = "1"
os.environ["TELEGRAM_BOT_TOKEN" if a == "coordinator" else f"{a.upper()}_TELEGRAM_BOT_TOKEN"] = "1234%d:test" % len(a)
from tabularium.agents import build
runtime = build(tmp, a)

from types import SimpleNamespace as NS
from unittest.mock import Mock
assert runtime.SYSTEM_PROMPT.count("## Everyday communication core")==1
assert runtime.SYSTEM_PROMPT.count("# Agent operating policy")==1
assert runtime.run_turn.__self__ is runtime
assert runtime._conversation_path(1).parent == tmp / 'task_state' / 'background' / '1'
assert callable(runtime.handle_message)
for provider in ('anthropic','openai'):
    tools=runtime.get_tools(provider)
    names={t.get('name') if provider=='anthropic' else t['function']['name'] for t in tools}
    assert 'remember' in names,(a,provider,names)
    assert 'write_memory' not in names,(a,provider)
assert runtime.AGENT_DIR == tmp
runtime.BACKGROUND_TASK_ID=''
from contextlib import nullcontext
responses = [
    NS(content=[NS(type='tool_use',id='t1',name='read_memory',input={})],stop_reason='tool_use',usage=None),
    NS(content=[NS(type='text',text='{"reply": "verified answer"}')],stop_reason='end_turn',usage=None)]
runtime.anthropic_client=NS(messages=NS(stream=Mock(side_effect=lambda **kw: nullcontext(NS(get_final_message=lambda: responses.pop(0))))))
runtime._execute_tool_unguarded=lambda name,inputs,chat_id: 'fixture memory'
messages=[{'role':'user','content':'Run a fixture tool'}]
runtime.conversations[1]=messages
assert runtime.run_turn_anthropic(messages,1)=='verified answer'
assert runtime.anthropic_client.messages.stream.call_count==2
assert runtime.anthropic_client.messages.stream.call_args_list[0].kwargs["system"][0]["text"] == runtime.SYSTEM_PROMPT
assert any(isinstance(m.get('content'),list) and any(b.get('type')=='tool_result' for b in m['content'] if isinstance(b,dict)) for m in messages)
assert runtime.tool_remember(kind='note',content='fixture durable note',chat_id=1)
assert 'fixture durable note' in (tmp/'memory.md').read_text(encoding='utf-8')
assert 'Tracking' in runtime.tool_remember(kind='thread',name='fixture',state='next',chat_id=1)
assert '[fixture]' in runtime._load_core_memory()
if a != 'private':
    runtime.bot.send_message=Mock()
    runtime.handle_threads_cmd(NS(chat=NS(id=1),text='/threads pin fixture'))
    runtime.handle_threads_cmd(NS(chat=NS(id=1),text='/threads unpin fixture'))
    runtime.handle_threads_cmd(NS(chat=NS(id=1),text='/threads drop fixture'))
    assert '[fixture]' not in runtime._load_core_memory()
print('RESULT ' + json.dumps({'startup':True,'turn':True,'memory':True,'tools':True}))
'''


class SmokeAgents(unittest.TestCase):
    def test_each_agent_loads_on_the_shared_engine(self):
        for a in AGENTS:
            r = subprocess.run([sys.executable, "-c", CHILD, str(ROOT), a], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=180)
            line = next((l for l in r.stdout.splitlines() if l.startswith("RESULT ")), None)
            self.assertIsNotNone(line, f"{a} failed to load:\n{r.stdout[-800:]}\n{r.stderr[-1200:]}")
            import json
            res = json.loads(line[7:])
            for k in ('startup','turn','memory','tools'):
                self.assertTrue(res[k], (a, k))



if __name__ == "__main__":
    unittest.main()
