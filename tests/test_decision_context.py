"""The Oct 26 deletion approval survives tool-heavy conversation and restart."""
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from tests.agent_harness import make_agent, put
from tabularium.records import task_decisions as td
from tabularium.memory.conversation_recall import ConversationRecall
from tabularium.core.outcome import Failed, Refused


class DecisionContext(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def agent(self, name='cato'):
        root = self.root / name
        root.mkdir(exist_ok=True)
        agent = make_agent(name, root)
        agent.send_message = lambda *a, **k: True
        return agent

    def question(self, agent):
        store = agent._background
        task = store.start(1, 'Redraw charts')['id']
        put(store, task, 'running')
        return store.ask(1, task, 'Delete the rendered images?')

    def say(self, agent, reply=900, text='yes'):
        message = SimpleNamespace(chat=SimpleNamespace(id=1), message_id=901,
            date=1793047939, text=text, reply_to_message=SimpleNamespace(message_id=reply))
        return agent._settle_by_reply(message)

    def test_pending_task_cannot_spawn_a_disconnected_approval(self):
        agent = self.agent()
        decision = self.question(agent)
        out = agent._park(1, 'send_email', {'to': 'test@example.test'}, 'Send?')
        self.assertIsInstance(out, Refused)
        self.assertIn('decision_id', str(out))
        self.assertEqual([r['id'] for r in td.pending(agent._background, 1)], [decision['id']])

    def test_one_decision_authorizes_exact_action_after_restart_and_resumes_once(self):
        agent = self.agent()
        decision = self.question(agent)
        td.bind_messages(agent._background, decision['id'], [800])
        action = {'to': 'test@example.test', 'subject': 'test', 'body': 'test'}
        prepared = td.execute(agent, 1, dict(action='prepare', decision_id=decision['id'],
            tool='send_email', inputs=action))
        self.assertNotIsInstance(prepared, Failed)
        row = td.pending(agent._background, 1)[0]
        self.assertEqual((row['id'], row['task'], row['kind']), (decision['id'], decision['task'], 'action'))
        self.assertIsNone(td.by_message(agent._background, 1, 800))
        td.bind_messages(agent._background, decision['id'], [900])
        restarted = self.agent()
        ran = []
        restarted.execute_tool = lambda name, inputs, chat: ran.append((name, inputs)) or 'sent'
        self.assertEqual(self.say(restarted), (True, ''))
        self.say(restarted)
        self.assertEqual(ran, [('send_email', action)])
        self.assertEqual(td.pending(restarted._background, 1), [])
        self.assertEqual(restarted._background.get(decision['task'])['status'], 'queued')
        controls = restarted._background.pending_controls(decision['task'])
        self.assertEqual(len(controls), 1)
        self.assertIn('sent', controls[0]['text'])

    def test_reply_cannot_recreate_question_after_preparing_its_action(self):
        from tabularium.core.reply_envelope import Reply
        agent = self.agent()
        decision = self.question(agent)
        td.execute(agent, 1, dict(action='prepare', decision_id=decision['id'],
            tool='send_email', inputs={'to': 'test@example.test', 'subject': 's', 'body': 'b'}))
        result = Reply('Please approve the exact action.', question=decision['question'],
                       decision_task_id=decision['task'], decision_id=decision['id'])
        td.verify_reply(agent, [], 1, result)
        self.assertEqual([r['id'] for r in td.pending(agent._background, 1)], [decision['id']])
        self.assertEqual(result.decision_id, decision['id'])

    def test_failed_action_is_recorded_without_claiming_done(self):
        agent = self.agent()
        decision = self.question(agent)
        td.execute(agent, 1, dict(action='prepare', decision_id=decision['id'],
            tool='send_email', inputs={'to': 'test@example.test', 'subject': 's', 'body': 'b'}))
        td.bind_messages(agent._background, decision['id'], [900])
        sent = []
        agent.send_message = lambda chat, text, **kw: sent.append(str(text))
        agent.execute_tool = lambda *a: Failed('Mail unavailable')
        self.say(agent)
        self.assertNotIn('Done.', str(sent))
        self.assertIn('Mail unavailable', str(sent))
        self.assertIn('Mail unavailable', agent._background.pending_controls(decision['task'])[0]['text'])

    def test_claimed_but_unrecorded_action_is_visible_and_never_replayed(self):
        agent = self.agent()
        decision = self.question(agent)
        td.execute(agent, 1, dict(action='prepare', decision_id=decision['id'],
            tool='send_email', inputs={'to': 'test@example.test', 'subject': 's', 'body': 'b'}))
        td.bind_messages(agent._background, decision['id'], [900])
        self.assertTrue(td.settle_action(agent._background, 1, decision['id'], True, 'fixture'))
        restarted = self.agent()
        ran = []
        restarted.execute_tool = lambda *a: ran.append(a)
        self.say(restarted)
        self.assertEqual(ran, [])
        self.assertIn('Outcome unknown', restarted._background.live_context(1))
        self.assertEqual(restarted._background.get(decision['task'])['status'], 'needs_attention')

    def test_cancelled_task_cannot_execute_a_prepared_action(self):
        agent = self.agent()
        decision = self.question(agent)
        td.execute(agent, 1, dict(action='prepare', decision_id=decision['id'],
            tool='send_email', inputs={'to': 'test@example.test', 'subject': 's', 'body': 'b'}))
        td.bind_messages(agent._background, decision['id'], [900])
        agent._background.move(decision['task'], 'cancel', 'cancelled')
        ran = []
        agent.execute_tool = lambda *a: ran.append(a)
        self.say(agent)
        self.assertEqual(ran, [])

    def test_preparation_cannot_widen_agent_scope_or_overwrite_an_action(self):
        agent = self.agent('tacitus')
        out = td.execute(agent, 1, dict(action='prepare', decision_id=0,
            tool='execute_command', inputs={'command': 'whoami'}))
        self.assertIsInstance(out, Failed)
        agent = self.agent('cato')
        decision = self.question(agent)
        td.execute(agent, 1, dict(action='prepare', decision_id=decision['id'],
            tool='send_email', inputs={'to': 'test@example.test', 'subject': 's', 'body': 'b'}))
        out = td.execute(agent, 1, dict(action='prepare', decision_id=decision['id'],
            tool='send_email', inputs={'to': 'different@example.test', 'subject': 's', 'body': 'b'}))
        self.assertIsInstance(out, Refused)
        self.assertNotIn('different@', td.pending(agent._background, 1)[0]['action'])

    def test_recent_instruction_and_approval_survive_tool_volume_for_every_agent(self):
        for name in ('cato', 'callimachus', 'vitruvius', 'tacitus'):
            agent = self.agent(name)
            history = [{'role': 'user', 'content': 'seed'}, {'role': 'assistant', 'content': 'Ready'}]
            history += [{'role': 'user', 'content': 'Delete the redraw.'},
                        {'role': 'assistant', 'content': 'Approved action: directory already absent.'},
                        {'role': 'user', 'content': 'Investigate another task'}]
            for n in range(60):
                history += [{'role': 'assistant', 'content': [{'type': 'tool_use', 'id': str(n),
                            'name': 'read_file', 'input': {'path': 'fixture'}}]},
                            {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': str(n),
                            'content': 'verbose irrelevant result ' * 300}]}]
            history += [{'role': 'assistant', 'content': 'Investigation complete.'},
                        {'role': 'user', 'content': 'What are these pending questions?'}]
            before = json.dumps(history)
            wire = agent._prepare_request_context(agent.trim_history(history, 1), 1)
            self.assertIn('Delete the redraw.', json.dumps(wire), name)
            self.assertIn('directory already absent', json.dumps(wire), name)
            self.assertEqual(json.dumps(history), before)
            self.assertLess(len(json.dumps(wire)), 35000)
            archives = list((agent.AGENT_DIR/'context_archive'/'1').glob('*.json'))
            self.assertEqual(len(archives), 1)
            trace = json.loads(archives[0].read_text(encoding='utf-8'))
            self.assertEqual(trace, history[5:-2])

    def test_followup_does_not_orphan_an_unfinished_tool_call(self):
        from tabularium.core.context_window import completed_exchanges
        history = [{'role': 'user', 'content': 'seed'}, {'role': 'assistant', 'content': 'Ready'},
            {'role': 'user', 'content': 'Start'},
            {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'pending', 'name': 'read_file', 'input': {}}]},
            {'role': 'user', 'content': 'Also preserve this requirement'},
            {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'pending', 'content': 'result'}]}]
        wire = completed_exchanges(history, self.root / 'archive')
        self.assertEqual(wire, history)

    def test_both_provider_formats_retain_approval_and_current_calls(self):
        from tabularium.models import subscription_runtime
        agent = self.agent()
        history = agent.load_memory_context() + [
            {'role': 'user', 'content': 'Delete the redraw.'},
            {'role': 'assistant', 'content': 'Folder already absent; approval recorded.'},
            {'role': 'user', 'content': 'Read the report'},
            {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'r', 'name': 'read_file', 'input': {'path': 'report'}}]},
            {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'r', 'content': 'Keep section B'}]}]
        wire = agent._prepare_request_context(agent.trim_history(history, 1), 1)
        for value in (subscription_runtime.sdk_tool_names(wire), agent._history_to_responses_items(wire)):
            self.assertIn('Delete the redraw.', str(value))
            self.assertIn('Keep section B', str(value))

    def test_ordinary_conversation_is_unchanged_and_canonical_history_is_verbatim(self):
        from tabularium.core.context_window import completed_exchanges
        agent = self.agent()
        history = agent.load_memory_context()
        for n in range(9):
            history += [{'role': 'user', 'content': f'Question {n}'},
                        {'role': 'assistant', 'content': f'Answer {n}'}]
        history.append({'role': 'user', 'content': 'Use the later requirement: euros, not dollars.'})
        self.assertEqual(completed_exchanges(history, self.root/'archive'), history)
        attach = agent._attach_current_context
        agent._attach_current_context = lambda value, chat: value
        try:
            self.assertEqual(agent.trim_history(history, 1), history)
        finally:
            agent._attach_current_context = attach
        agent.conversations[1] = history
        agent.save_conversation(1)
        self.assertEqual(json.loads(agent._conversation_path(1).read_text(encoding='utf-8')), history)

    def test_window_prefix_stays_anchored_between_exchange_cuts(self):
        agent = self.agent()
        agent._attach_current_context = lambda value, chat: value
        history = agent.load_memory_context()
        for n in range(24):
            history += [{'role': 'user', 'content': f'Question {n}'},
                        {'role': 'assistant', 'content': f'Answer {n}'}]
        first = agent.trim_history(history, 1)
        history += [{'role': 'user', 'content': 'One more question'}]
        second = agent.trim_history(history, 1)
        self.assertEqual(second[:-1], first)

    def test_his_answers_bind_every_later_turn_and_worker_of_the_chat(self):
        """2026-11-08: the same refused option came back four times, from the foreground and from a worker that
        never saw his no. An answered question is a ruling in the live evidence of every turn and task."""
        agent = self.agent()
        store = agent._background
        first = store.start(1, 'Fix the arrow')['id']; put(store, first, 'running')
        asked = store.ask(1, first, 'Delete the left red piece?')
        self.assertNotIn('owner_rulings', store.live_context(1))                 # pending: a question, not a ruling
        td.answer_record(store, 1, asked['id'], 'No. It stays behind the ring; align it.', 'synthetic verified source')
        for scope in (None, first, store.start(1, 'Four new directions')['id']):
            evidence = json.loads(store.live_context(1, scope).split(chr(10), 1)[1])
            self.assertEqual(evidence['owner_rulings']['answers'][0]['answer'], 'No. It stays behind the ring; align it.', scope)
            self.assertIn('never offer again', evidence['owner_rulings']['note'])

    def test_empty_keyword_search_returns_labelled_recent_evidence_not_false_absence(self):
        recall = ConversationRecall(self.root)
        recall.record(1, [{'role':'user','content':'[2026-10-26 Monday 20:51] Delete the redraw.'}], self.root/'conversation_1.json')
        result = recall.search(1, query='删', date_from='2026-10-26', speaker='user')
        self.assertEqual(result['matches'], [])
        self.assertIn('Delete the redraw.', str(result['recent_messages']))
        self.assertIn('not evidence', result['interpretation'])


if __name__ == '__main__':
    unittest.main()
