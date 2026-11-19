"""A skill-edit verdict is a field, and 'no question asked' is None (2026-11-19).

Alex's five Apply clicks sat on his list for three days: the buttons sent decision_id 0, which the ingress
accepted as a question identity and then queued as ordinary chat, where a second click or a note he typed was
folded into the same bubble and no longer matched the single-line prose the verdict was read back from.
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from tabularium.channels.messaging import decode
from tabularium.core.agent_runtime import DurableInbox
from tabularium.channels.owner_telegram import OWNER_CHAT_ID
from tabularium.learning import learning


def payload(inbox, job):
    import json
    with inbox.db() as db:
        row = db.execute('SELECT handler,payload FROM jobs WHERE id=?', (job,)).fetchone()
    return row[0], json.loads(row[1])


class SkillEditVerdict(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.inbox = DurableInbox(Path(self.tmp.name), Mock())
        self.edit = {'key': 'lesson-991d767616c2ab32', 'verdict': 'apply it'}

    def test_zero_is_not_a_question_identity(self):
        """0 passed the check and then routed as chat: absent is None and nothing else."""
        self.assertRaises(ValueError, self.inbox.accept_dashboard, 'x', 'Yes', 0)
        self.assertRaises(ValueError, self.inbox.accept_dashboard, 'y', 'Yes', -1)
        self.assertRaises(ValueError, self.inbox.accept_dashboard, 'z', 'Yes', '9')

    def test_absent_is_chat_and_a_question_identity_is_a_reply(self):
        handler, _ = payload(self.inbox, self.inbox.accept_dashboard('a', 'Hello')['job'])
        self.assertEqual(handler, 'handle_message')
        handler, _ = payload(self.inbox, self.inbox.accept_dashboard('b', 'Yes', 9)['job'])
        self.assertEqual(handler, 'handle_dashboard_reply')

    def test_a_verdict_is_never_folded_into_the_next_bubble(self):
        """follow_ups gathers handle_message jobs only, so two clicks cannot join into one text."""
        first = self.inbox.accept_dashboard('v1', 'Skill edit lesson-aaa: apply it.', skill_edit=self.edit)
        self.inbox.accept_dashboard('v2', 'Skill edit lesson-bbb: drop it.',
                                    skill_edit={'key': 'lesson-bbb', 'verdict': 'drop it'})
        handler, stored = payload(self.inbox, first['job'])
        self.assertEqual(handler, 'handle_dashboard_reply')
        self.assertEqual(stored['skill_edit'], self.edit)
        self.assertEqual(self.inbox.follow_ups(OWNER_CHAT_ID, first['job']), [])

    def test_a_malformed_verdict_is_refused(self):
        for bad in ({'key': 'lesson-a', 'verdict': 'apply'}, {'key': 'nope', 'verdict': 'apply it'},
                    {'key': 'lesson-a'}, {'key': 'lesson-a', 'verdict': 'apply it', 'extra': 1}, 'apply it'):
            self.assertRaises(ValueError, self.inbox.accept_dashboard, f'bad-{bad}', 'x', None, bad)
        self.assertRaises(ValueError, self.inbox.accept_dashboard, 'both', 'x', 9, self.edit)

    def test_the_note_he_types_reaches_the_agent_and_changes_no_verdict(self):
        job = self.inbox.accept_dashboard('n', 'Skill edit lesson-aaa: apply it.\nMind the tone.',
                                          skill_edit=self.edit)['job']
        _, stored = payload(self.inbox, job)
        self.assertEqual(stored['skill_edit'], self.edit)
        self.assertIn('Mind the tone.', stored['text'])
        self.assertIsNone(learning.GO.match(stored['text']))      # the prose no longer has to parse

    def test_decode_carries_the_verdict_and_reads_legacy_zero_as_absent(self):
        _, stored = payload(self.inbox, self.inbox.accept_dashboard('d', 'x', skill_edit=self.edit)['job'])
        self.assertEqual(decode(stored).skill_edit, self.edit)
        self.assertIsNone(decode(stored).decision_id)
        legacy = dict(stored, decision_id=0, skill_edit=None)     # rows written before this change
        self.assertIsNone(decode(legacy).decision_id)

    def test_typed_telegram_verdicts_still_parse(self):
        go = learning.GO.match('Skill edit lesson-aaa: apply it, no trial needed.')
        self.assertEqual((go.group(1), go.group(2)), ('lesson-aaa', 'apply it, no trial needed'))
        self.assertEqual(learning.GO.match('Skill edit lesson-aaa: apply it.').group(2), 'apply it')
        for verdict in learning.VERDICTS:
            self.assertIsNotNone(learning.GO.match(f'Skill edit lesson-a: {verdict}'))

class SettleFromTheField(unittest.TestCase):
    """The card's verdict settles from its fields, whatever the text around it says."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def proposed(self, agent):
        skill = agent.AGENT_DIR / 'skills' / 'basket' / 'SKILL.md'
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text(
            "\n".join(['---', 'name: basket', 'metadata:', '  version: "1.0.0"', '---', '', '## Rules', '- Read the archives.', '']),
            encoding='utf-8')
        source = f"message:{agent._recall.note(OWNER_CHAT_ID, 'Freeze it first', 'telegram')}"
        key = learning.propose(agent.AGENT_DIR, OWNER_CHAT_ID, 'Freeze membership first', source, 'basket_build')
        learning.propose_change(agent.AGENT_DIR, OWNER_CHAT_ID, key, skill, '- Read the archives.', '- Freeze, then read.',
                                {'claim': 'The rules read the archives before the membership is fixed.',
                                 'anchor': '- Read the archives.', 'deletes': 'Read the archives',
                                 'alternatives': ['a', 'b']})
        return key, skill

    def test_a_note_and_a_folded_second_click_cannot_change_the_verdict(self):
        import time
        from tests.agent_harness import make_agent
        agent = make_agent('coordinator', self.root)
        agent.send_message = Mock()
        key, skill = self.proposed(agent)
        raw = {'channel': 'dashboard', 'message_id': -1, 'date': time.time(),
               'text': chr(10).join([f'Skill edit {key}: apply it, no trial needed.', 'Mind the tone.',
                                     f'Skill edit {key}: drop it.']),
               'skill_edit': {'key': key, 'verdict': 'apply it, no trial needed'}}
        self.assertIsNone(learning.GO.match(raw['text']))         # the prose this text would have parsed to: none
        self.assertTrue(agent._settle_by_reply(decode(raw))[0])
        self.assertIn('applied to SKILL.md, no trial', agent.send_message.call_args[0][1])
        self.assertIn('Freeze, then read', skill.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
