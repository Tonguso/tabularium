"""Manual live before/after probe: two Cal subscription calls; no API fallback, memory or tools.
Run with the agent interpreter. Baseline 9198d70, synthetic quality oracle and usage only.
"""
from pathlib import Path
import tempfile,json,subprocess,importlib.util
from types import SimpleNamespace
from openai import OpenAI
from tabularium.models.openai_chatgpt import ChatGPTPlan
from tabularium.core.shared_handlers import SharedHandlers
from tabularium.core.reply_envelope import reply
root=Path(__file__).resolve().parents[1]
scratch=tempfile.TemporaryDirectory()
old=Path(scratch.name)/'old_shared.py'
old.write_bytes(subprocess.check_output(['git','show','9198d70:agent_core/shared_handlers.py'],cwd=root))
spec=importlib.util.spec_from_file_location('old_shared',old); baseline=importlib.util.module_from_spec(spec);spec.loader.exec_module(baseline)
agent=SimpleNamespace(_inbox=SimpleNamespace(record_event=lambda *a,**k:None),_cache_key=lambda:'cal-accounting-comparison-20261024',OPENAI_REASONING_EFFORT='medium',config=SimpleNamespace(name='librarian',display='Cal'))
client=OpenAI(api_key=ChatGPTPlan('Callimachus').access_token(),max_retries=0,timeout=90)
content='\n'.join(f'Record {i}: synthetic balance {i*7} GBP.' for i in range(300))
content+='\nReport the balance for record 299, the currency, and whether deployment is allowed when a required test failed. No tools.'
result={}
try:
    for label,send in [('before',baseline.SharedHandlers._responses_call),('after',SharedHandlers._responses_call)]:
        response,text,items,events=send(agent,client,model='gpt-6.1-sol',instructions='Answer only from the synthetic evidence. Deployment requires all required tests passing.',input_items=[dict(role='user',content=content)],tools=[],plan_mode=True)
        answer=str(reply(text))
        result[label]={'usage':{k:v for k,v in response.usage.model_dump().items() if k!='attribution'},'answer':answer,'quality':{'balance':'2093' in answer.replace(',',''),'currency':'GBP' in answer,'blocked':any(w in answer.lower() for w in ['not allowed','not permitted','blocked','cannot','must not'])}}
        print(label,json.dumps(result[label]),flush=True)
finally:
    client.close();scratch.cleanup()
    (root/'coordinator/reports/cache-accounting-comparison.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
