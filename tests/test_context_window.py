"""Context window: archiving, retries and model loops keep the task, its findings and tool pairs (real Cato agent).
Rewritten 2026-10-14 from lifted functions in a hand-made scope to the production object with scripted model calls."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
from agent_harness import make_agent
from tabularium.core.context_window import prepare_context, archive_preview, wire_size, ARCHIVE_MARKER


def scripted_agent(root):
    """A real foreground Cato whose outside world is quiet: no tools offered, no inbox, no saving, no memory."""
    a = make_agent("coordinator", root)
    a.get_tools = lambda provider: []
    a.get_anthropic_model = lambda chat: "test-model"
    a.SYSTEM_PROMPT = "test instructions"
    a.OVERLOAD_FALLBACK_MODELS = []
    a.MAX_TOOL_ITERATIONS = 40
    a._inbox = NS(progress=lambda *x, **k: None, record_event=lambda *x, **k: None)
    a.save_conversation = lambda *x: None
    a._load_core_memory = lambda: ""
    return a


def engine_of(agent):
    """The engine namespace the agent's methods run in (the harness loads a fresh engine per agent), as an object
    whose attributes are its globals, so tests patch exactly what the method sees."""
    return NS(**agent.run_turn_anthropic.__func__.__globals__)


def history(rounds=15):
    msgs = [{'role':'user','content':'CORE MEMORY'}, {'role':'assistant','content':'Ready'},
            {'role':'user','content':[{'type':'text','text':'CURRENT TASK: compare every report; do not resend email.'},
                                     {'type':'image','source':{'type':'base64','media_type':'image/png','data':'USER_IMAGE'}}]}]
    for i in range(rounds):
        name = 'skill' if i == 1 else 'read_research_reports'
        msgs.extend([
            {'role':'assistant','content':[{'type':'text','text':f'Finding {i}: evidence and uncertainty.'},
                {'type':'tool_use','id':str(i),'name':name,'input':{'page':i}}]},
            {'role':'user','content':[{'type':'tool_result','tool_use_id':str(i),
                'content':f'HEAD {i}\n'+('x'*40000)+f'UNIQUE MIDDLE {i}'+('y'*40000)+f'\nTAIL {i}'}]}])
    return msgs


def pairs(msgs):
    uses, results = [], []
    for m in msgs:
        for b in m['content'] if isinstance(m['content'], list) else []:
            if b.get('type')=='tool_use': uses.append(b['id'])
            if b.get('type')=='tool_result': results.append(b['tool_use_id'])
    return uses, results


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.agent = scripted_agent(self.root)

    def test_small_requests_unchanged(self):
        msgs = history(0)
        self.assertIs(prepare_context(msgs, self.root/'archive'), msgs)
        self.assertFalse((self.root/'archive').exists())

    def test_large_task_keeps_instructions_findings_pairs_recent_results_and_originals(self):
        msgs = history(35)
        original = deepcopy(msgs)
        compact = prepare_context(msgs, self.root/'archive')
        self.assertEqual(msgs, original)
        self.assertEqual(len(compact), len(msgs))
        self.assertEqual(compact[:3], original[:3])
        self.assertEqual(compact[-6:], original[-6:])
        self.assertEqual(pairs(compact), pairs(original))
        for a, b in zip(compact, original):
            if a['role']=='assistant': self.assertEqual(a,b)
        self.assertEqual(compact[6], original[6])  # loaded skill stays complete
        self.assertLess(wire_size(compact), wire_size(original))
        preview = compact[4]['content'][0]['content']
        path = Path(preview.split(']\n',1)[0][len(ARCHIVE_MARKER):])
        self.assertEqual(path.read_text(encoding='utf-8'), original[4]['content'][0]['content'])
        retrieved = self.agent.tool_read_file(str(path), 39000, 4000)
        self.assertIn('UNIQUE MIDDLE 0', retrieved)

    def test_retry_keeps_request_newest_result_and_side_effect_confirmation(self):
        msgs = history(10)
        msgs[8]['content'][0]['content'] = 'Email sent successfully. Message id=123.'
        original = deepcopy(msgs)
        compact = self.agent._compact_context_retry(msgs, 7)
        self.assertEqual(msgs, original)
        self.assertEqual(compact[:3], original[:3])
        self.assertEqual(compact[-2:], original[-2:])
        self.assertEqual(compact[8], original[8])
        self.assertLess(wire_size(compact), wire_size(msgs))

    def test_archive_write_failure_never_discards_content(self):
        msgs = history(10)
        original = deepcopy(msgs)
        with patch.object(Path, 'write_text', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): prepare_context(msgs, self.root/'archive')
        self.assertEqual(msgs, original)

    def test_multimodal_tool_archive_is_exact(self):
        content = [{'type':'image','source':{'type':'base64','media_type':'image/png','data':'original-image'}},
                   {'type':'text','text':'Visual evidence'}]
        preview = archive_preview(content, self.root/'archive')
        path = Path(preview.split(']\n',1)[0][len(ARCHIVE_MARKER):])
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), content)
        self.assertIn('Visual evidence',preview)

    def test_cache_markers_do_not_accumulate_and_tool_inputs_unchanged(self):
        msgs = history(3)
        msgs[3]['content'][1]['input']['cache_control'] = 'a real tool parameter'
        original = deepcopy(msgs)
        wire = msgs
        for _ in range(30):
            wire = self.agent._apply_cache_to_history(wire)
            count = sum('cache_control' in b for m in wire for b in m['content'] if isinstance(b,dict))
            self.assertEqual(count, 2)  # two more are attached to system/tools at request time
            wire.append({'role':'user','content':'another turn'})
        self.assertEqual(msgs, original)
        self.assertEqual(wire[3]['content'][1]['input']['cache_control'], 'a real tool parameter')

    def test_unreducible_context_fails_explicitly(self):
        msgs = history(0)
        original = deepcopy(msgs)
        with self.assertRaisesRegex(RuntimeError, 'History is preserved'):
            self.agent._compact_context_retry(msgs, 7)
        self.assertEqual(msgs, original)

    def test_tool_cap_archives_full_output_and_paging_reaches_tail(self):
        full = 'z'*80000 + 'EXACT FINAL NUMBER=743'
        preview = self.agent._cap_result(full)
        self.assertLess(len(preview),16000)
        path = Path(preview.split(']\n',1)[0][len(ARCHIVE_MARKER):])
        self.assertEqual(path.read_text(encoding='utf-8'),full)
        self.assertIn('EXACT FINAL NUMBER=743', self.agent.tool_read_file(str(path),79000))

    def test_anthropic_long_loop_context_retry_never_loses_task_or_replays_tools(self):
        agent = self.agent
        class APIError(Exception):
            status_code = 400
        executed, requests = [], []
        rejected = False
        def stream(**kwargs):
            nonlocal rejected
            requests.append(deepcopy(kwargs['messages']))
            if len(executed)==15 and not rejected:
                rejected=True
                raise APIError('prompt is too long')
            i = len(executed)
            if i < 30:
                response = NS(content=[NS(type='tool_use',id=str(i),name='read_research_reports',input={'page':i})],stop_reason='tool_use')
            else:
                response = NS(content=[NS(type='text',text='{"reply": "finished"}')],stop_reason='end_turn')
            class Stream:
                def __enter__(self): return self
                def __exit__(self,*args): pass
                def get_final_message(self): return response
            return Stream()
        agent.anthropic_client = NS(messages=NS(stream=stream))
        agent._clean_content = lambda blocks: [vars(b).copy() for b in blocks]
        agent.execute_tool = lambda name, args, chat: (executed.append(args['page']) or ('report data ' * 8000))
        msgs = history(0)
        with patch.object(engine_of(agent).anthropic, 'APIStatusError', APIError):
            self.assertEqual(agent.run_turn_anthropic(msgs, 7), 'finished')
        self.assertEqual(executed,list(range(30)))
        self.assertTrue(rejected)
        for request in requests:
            self.assertIn('CURRENT TASK:',json.dumps(request))
            self.assertEqual(*pairs(request))
        self.assertGreater(len(requests[-1]),52)

    def test_plan_to_api_fallback_uses_context_guard(self):
        agent = self.agent
        class Quota(Exception): pass
        routes=[]
        notices=[]
        def call(client, **kwargs):
            routes.append((kwargs['plan_mode'],deepcopy(kwargs['input_items'])))
            if len(routes)==1: raise Quota()
            if len(routes)==2: raise RuntimeError('context_length_exceeded')
            response=NS(output=[NS(type='message',content=[NS(type='output_text',text='{"reply": "finished"}')])],status='completed')
            return response,'',[],[]
        for name, value in dict(_ChatGPTQuotaReached=Quota,
                _chatgpt_plan=NS(access_token=lambda: 'mock-token', mark_quota_exhausted=lambda: None),
                OPENAI_API_KEY='mock-api-key', CHATGPT_PLAN_MODEL='plan-test', get_openai_model=lambda chat: 'api-test',
                _oa_tools_for_responses=lambda: [], _responses_call=call,
                send_message=lambda chat, text: notices.append(text)).items():
            setattr(agent, name, value)
        with patch.dict(sys.modules, {'openai': NS(OpenAI=lambda **kw: NS())}):
            self.assertEqual(agent.run_turn_openai(history(10), 7), 'finished')
        self.assertEqual([r[0] for r in routes],[True,False,False])
        self.assertEqual(len(notices),1)
        self.assertIn("paid OpenAI API",notices[0])
        self.assertLess(wire_size(routes[-1][1]),wire_size(routes[-2][1]))
        for _,items in routes: self.assertIn('CURRENT TASK:',json.dumps(items))

    def test_context_and_overload_retries_do_not_force_model_switch(self):
        agent = self.agent
        class APIError(Exception):
            def __init__(self,status,message):
                self.status_code=status
                super().__init__(message)
        calls=[]
        def stream(**kwargs):
            calls.append(kwargs['model'])
            if len(calls)==1: raise APIError(529,'overloaded')
            if len(calls)==2: raise APIError(400,'prompt is too long')
            if len(calls)==3: raise APIError(529,'overloaded')
            class Stream:
                def __enter__(self): return self
                def __exit__(self,*args): pass
                def get_final_message(self):
                    return NS(content=[NS(type='text',text='{"reply": "finished"}')],stop_reason='end_turn')
            return Stream()
        agent.anthropic_client = NS(messages=NS(stream=stream))
        agent.OVERLOAD_FALLBACK_MODELS = ['fallback']
        eng = engine_of(agent)
        with patch.object(eng.anthropic, 'APIStatusError', APIError), patch.object(eng.time, 'sleep', lambda s: None):
            self.assertEqual(agent.run_turn_anthropic(history(10), 7), 'finished')
        self.assertEqual(calls,['test-model']*4)

    def test_expired_token_refresh_stays_on_subscription(self):
        agent = self.agent
        class AuthError(Exception):
            status_code=401
        class Quota(Exception): pass
        calls=[]
        refreshes=[]
        def token(force_refresh=False):
            refreshes.append(force_refresh)
            return 'refreshed' if force_refresh else 'expired'
        def call(client, **kwargs):
            calls.append((client.token,kwargs['plan_mode']))
            if len(calls)==1: raise AuthError('token_expired')
            return NS(output=[NS(type='message',content=[NS(type='output_text',text='{"reply": "finished"}')])],status='completed'),'',[],[]
        for name, value in dict(_ChatGPTQuotaReached=Quota, _chatgpt_plan=NS(access_token=token, mark_plan_available=lambda: False),
                OPENAI_API_KEY='mock-api', CHATGPT_PLAN_MODEL='gpt-6.1-sol', get_openai_model=lambda chat: 'gpt-6.1-sol',
                _oa_tools_for_responses=lambda: [], _responses_call=call,
                send_message=lambda *args: self.fail('Expiry must not trigger a billing notice')).items():
            setattr(agent, name, value)
        with patch.dict(sys.modules, {'openai': NS(OpenAI=lambda **kw: NS(token=kw['api_key']))}):
            self.assertEqual(agent.run_turn_openai(history(0), 7), 'finished')
        self.assertEqual(calls,[('expired',True),('refreshed',True)])
        self.assertEqual(refreshes,[False,True])



if __name__=='__main__':
    unittest.main(verbosity=2)
