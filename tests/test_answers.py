"""A reply says which of Alex's messages it answers (2026-10-21).

17:44-17:45 Alex sent three messages: Cal should study how academia does it; or how mature quant strategies do
it; is Vit's app done. The first started Cato's turn, the other two joined it. Cato's reply answered the third only
and quoted the first. Now the reply lists what it answers; a message left out bounces the reply once, and the quote
goes on the answered message only when it is not Alex's latest."""
import contextlib, io, json, tempfile, time, unittest
from pathlib import Path
from unittest.mock import Mock

from agent_harness import make_agent
from tabularium.core import reply_envelope
from tabularium.core.reply_envelope import Reply
from tabularium.core.turn_loop import run, Exchange
from tabularium.channels.owner_telegram import OWNER_CHAT_ID
CHAT = OWNER_CHAT_ID
TURN = [(11946, "Cal是不是应该去查一下公开资料"), (11947, "或者说，成熟的做法通常是怎样的"),
        (11948, "Vit的app更新了吗")]


class Answers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / "coordinator"; root.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            self.agent = make_agent("coordinator", root)
        self.agent.BACKGROUND_TASK_ID = ""
        self.agent.bot = Mock()
        inbox = self.agent._inbox
        with inbox.db() as db:
            for mid, text in TURN:
                db.execute("INSERT INTO jobs(chat,message,handler,payload,status,created) VALUES (?,?,?,?,?,?)",
                           (CHAT, mid, "handle_message", json.dumps({"message_id": mid, "text": text}), "done", time.time()))
        inbox.local.message_id = 11946
        inbox.local.turn_messages = list(TURN)

    def quoted(self, reply):
        self.agent.send_message(CHAT, reply)
        return self.agent.bot.send_message.call_args.kwargs.get("reply_to_message_id")

    def test_the_answer_to_the_latest_message_is_not_quoted(self):
        self.assertIsNone(self.quoted(Reply("界面有了，端到端还没过关。", answers=[11948])))

    def test_an_answer_to_an_earlier_message_quotes_it(self):
        self.assertEqual(self.quoted(Reply("对，已让 Cal 去查公开资料。", answers=[11946])), 11946)

    def test_a_reply_covering_all_three_is_not_quoted(self):
        self.assertIsNone(self.quoted(Reply("…", answers=[11946,11947,11948])))

    def test_coverage_is_checked_only_when_the_turn_took_in_more_than_one(self):
        missing = reply_envelope.unanswered(Reply("x", answers=[11948]), TURN)
        self.assertEqual([mid for mid, _ in missing], [11946, 11947])
        self.assertEqual(reply_envelope.unanswered(Reply("x", answers=[11946,11947,11948]), TURN), [])
        self.assertEqual(reply_envelope.unanswered(Reply("x"), TURN[:1]), [])

    def test_the_turn_bounces_once_when_messages_are_left_out(self):
        self.agent._background.activated_since = lambda chat, since: False
        seen = []
        class Provider:
            calls = 0
            def exchange(self, history):
                self.calls += 1
                seen.append(json.dumps(history[-1], ensure_ascii=False))
                answers = [11948] if self.calls == 1 else [11946,11947,11948]
                return Exchange(text=json.dumps({"reply": f"reply {self.calls}", "modality": "text", "work": "none",
                                                 "answers": answers}, ensure_ascii=False))
        provider = Provider()
        messages = [{"role": "user", "content": TURN[0][1]}]
        out = run(self.agent, messages, CHAT, provider)
        self.assertEqual((str(out), provider.calls), ("reply 2", 2))
        self.assertIn("does not cover", seen[1])
        self.assertIn("或者说，成熟的做法通常是怎样的", seen[1])
        self.assertNotIn("Vit的app更新了吗", seen[1].split("does not cover")[1])


    def test_a_wrong_decision_declaration_bounces_once_and_never_ends_the_turn(self):
        # 2026-10-27 23:27: Cato declared decision 74 with a new question; the refusal reached Alex as "Task stopped".
        store = self.agent._background
        store.ask(CHAT, 0, "Keep the overlays unmasked?")
        pending = store.decisions(CHAT, 0)[0]["id"]
        seen = []
        class Provider:
            calls = 0
            def exchange(self, history):
                self.calls += 1
                seen.append(json.dumps(history[-1], ensure_ascii=False))
                return Exchange(text=json.dumps({"reply": f"reply {self.calls}", "modality": "text", "work": "none", "answers": [11946, 11947, 11948],
                                                 "question": "Discuss Kabuki now?", "decision_id": pending, "decision_task_id": 0}, ensure_ascii=False))
        provider = Provider()
        messages = [{"role": "user", "content": TURN[0][1]}]
        out = run(self.agent, messages, CHAT, provider)
        self.assertEqual((str(out), provider.calls), ("reply 2", 2))
        self.assertIn("asks a different question", seen[1])
        from tabularium.records.task_decisions import verify_reply
        result = verify_reply(self.agent, messages, CHAT, out)          # still wrong after the bounce: a new question, no crash
        self.assertNotEqual(result.decision_id, pending)
        questions = {d["question"]: d["id"] for d in store.decisions(CHAT, 0)}
        self.assertEqual(questions["Discuss Kabuki now?"], result.decision_id)
        # His message moved past the earlier conversation question: out of the evidence, still in the record (2026-11-11).
        from tabularium.records.task_decisions import lapsed
        self.assertNotIn("Keep the overlays unmasked?", questions)
        self.assertEqual([d["question"] for d in lapsed(store, CHAT)], ["Keep the overlays unmasked?"])

    def test_one_reply_covers_original_and_correction_but_not_unrelated_request(self):
        turn=[(101,'Save result.json with ready'),(102,'Change that to revised'),(103,'Also tell me the weather')]
        reply=reply_envelope.reply({'reply':'Saved the revised file.','answers':[101,102]})
        self.assertEqual(reply_envelope.answered(reply,turn),turn[:2])
        self.assertEqual(reply_envelope.unanswered(reply,turn),turn[2:])

    def test_identical_texts_and_prefixes_do_not_confuse_message_identity(self):
        turn=[(101,'Check the result'),(102,'Check the result'),(103,'Check the other result')]
        self.assertEqual(reply_envelope.unanswered(Reply('Checked.',answers=[102]),turn),[turn[0],turn[2]])
        self.assertEqual(reply_envelope.answered(Reply('Checked.',answers=['Check']),turn),[])

    def test_invalid_and_old_ids_do_not_claim_current_messages(self):
        turn=[(1,'First'),(2,'Second')]
        for ids in ([True],[1.0],['1'],[999],['First']):
            self.assertEqual(reply_envelope.unanswered(Reply('Done',answers=ids),turn),turn,ids)
        self.assertEqual(reply_envelope.answered(Reply('Done',answers=[2,2,999]),turn),[turn[1]])
        self.assertEqual(reply_envelope.answered(Reply('Done',answers=[1]),[(3,'First'),(4,'Second')]),[])

    def test_acknowledgement_does_not_prove_deliverable_completion(self):
        reply=Reply('Saved',answers=[11946,11947,11948],task_outcome='completed')
        self.assertEqual(reply_envelope.unanswered(reply,TURN),[])
        path=self.agent.AGENT_DIR/'missing.txt'
        self.agent._background.checkpoint(1,1,'','',[],guesses=[],completion_items=[{
            'item':'Saved result','check':'Read real file','verification':{'kind':'contains','paths':[str(path)],'text':'correct'}}])
        self.assertEqual(self.agent._background.finish(1,reply),'needs_attention')


    def test_all_providers_request_missing_ids_without_saving_runtime_feedback(self):
        from unittest.mock import patch, MagicMock
        from types import SimpleNamespace as NS
        from tabularium.core.roster import AGENTS
        import sqlite3
        from tabularium.models import subscription_runtime as bridge
        import claude_agent_sdk as sdk
        for name in AGENTS:
            for route in ('openai','anthropic','claude'):
                root=Path(self.tmp.name)/(name+route);root.mkdir()
                agent=make_agent(name,root)
                agent._inbox.local.turn_messages=list(TURN)
                db=sqlite3.connect(agent._inbox.database)
                try:
                    for mid,text in TURN:
                        db.execute('INSERT INTO jobs(chat,message,handler,payload,status,created) VALUES (?,?,?,?,?,?)',
                                   (CHAT,mid,'handle_message',json.dumps({'message_id':mid,'text':text}),'running',time.time()))
                    db.commit()
                finally:
                    db.close()
                messages=[{'role':'user','content':text} for mid,text in TURN]
                agent.conversations[CHAT]=messages
                wires=[]
                def payload(wire):
                    wires.append(str(wire))
                    return json.dumps(dict(reply='All requests handled' if len(wires)>1 else 'App status only',
                                           answers=[11946,11947,11948] if len(wires)>1 else [11948]))
                def response(client,**kw):
                    block=NS(type='message',content=[NS(type='output_text',text=payload(kw))])
                    return NS(output=[block],status='completed',incomplete_details=None,usage=None),'',[],[]
                def stream(**kw):
                    context=MagicMock()
                    context.__enter__.return_value.get_final_message.return_value=NS(
                        content=[NS(type='text',text=payload(kw))],stop_reason='end_turn',usage=None)
                    return context
                async def query(prompt,options):
                    wire=[m async for m in prompt]
                    yield sdk.ResultMessage(subtype='success',duration_ms=1,duration_api_ms=1,is_error=False,
                                            num_turns=1,session_id='test',structured_output=json.loads(payload(wire)))
                patches=[]
                if route=='openai':
                    agent.OPENAI_API_KEY='test';agent._responses_call=response
                    token=agent._routing.ACTIVE.set({'kind':'openai_api','model':'gpt-test'})
                    patches.append(patch.object(agent._routing,'run',side_effect=lambda a,m,c:a.run_turn_openai(m,c)))
                elif route=='anthropic':
                    agent.anthropic_client=NS(messages=NS(stream=stream))
                    patches.append(patch.object(agent._routing,'run',side_effect=lambda a,m,c:a.run_turn_anthropic(m,c)))
                else:
                    patches.extend([patch.object(bridge,'find_cli',return_value='fake.exe'),
                                    patch.object(bridge,'subscription_ready',return_value=(True,'')),
                                    patch.object(sdk,'query',side_effect=query),
                                    patch.object(agent._routing,'run',side_effect=lambda a,m,c:bridge.run_claude(a,m,c,'test'))])
                for p in patches:p.start()
                try:
                    result=agent.run_turn(messages,CHAT)
                finally:
                    for p in reversed(patches):p.stop()
                    if route=='openai':agent._routing.ACTIVE.reset(token)
                self.assertEqual(reply_envelope.unanswered(result,TURN),[],(name,route))
                self.assertEqual(len(wires),2,(name,route))
                self.assertIn('11946',wires[0])
                self.assertIn('11947',wires[0])
                self.assertIn('11948',wires[0])
                self.assertIn('ID 11946',wires[1])
                self.assertIn('ID 11947',wires[1])
                self.assertNotIn('Runtime feedback',json.dumps(messages))


if __name__ == "__main__":
    unittest.main()
