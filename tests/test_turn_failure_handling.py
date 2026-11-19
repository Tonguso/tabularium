"""A failed turn leaves nothing silently complete (2026-11-19).

Alex asked two questions at 20:40. The Claude Code route timed out at step zero, the turn stopped, and the
second question, picked up mid-turn and marked done on pickup, was neither answered nor on any recovery list.
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from tabularium.core.agent_runtime import DurableInbox
from tabularium.channels.owner_telegram import OWNER_CHAT_ID
from tabularium.models.provider_routing import failure, timed_out_before_acting, Unavailable


def status(inbox, job):
    with inbox.db() as db:
        return db.execute('SELECT status FROM jobs WHERE id=?', (job,)).fetchone()[0]


class AbsorbedFollowUpSettlesWithItsTurn(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.inbox = DurableInbox(Path(self.tmp.name), Mock())
        self.inbox.local.absorbed = []

    def queued(self, name, text):
        return self.inbox.accept_dashboard(name, text)['job']

    def test_absorb_claims_instead_of_completing(self):
        job = self.queued('a', 'second question')
        self.inbox.absorb([job])
        self.assertEqual(status(self.inbox, job), 'running')      # not 'done': the turn has not answered yet
        self.assertEqual(self.inbox.local.absorbed, [job])

    def test_a_failed_turn_leaves_the_follow_up_on_the_recovery_list(self):
        first, second = self.queued('a', 'first'), self.queued('b', 'second')
        self.inbox.absorb([second])
        with self.inbox.db() as db:                                # the turn claims its own job, then fails
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (first,))
            settled = [first] + [i for i in self.inbox.local.absorbed if i != first]
            db.executemany("UPDATE jobs SET status=? WHERE id=? AND status IN ('running','pending')",
                           [('failed', i) for i in settled])
        self.assertEqual(status(self.inbox, second), 'failed')
        note = self.inbox.recovery_note(OWNER_CHAT_ID)
        self.assertIn(f'Task #{second}, failed', note)             # it reaches him instead of vanishing
        self.assertIn(f'Task #{first}, failed', note)

    def test_a_finished_turn_still_completes_it(self):
        job = self.queued('a', 'second')
        self.inbox.absorb([job])
        with self.inbox.db() as db:
            db.executemany("UPDATE jobs SET status=? WHERE id=? AND status IN ('running','pending')", [('done', job)])
        self.assertEqual(status(self.inbox, job), 'done')


class TimeoutFallsThroughOnlyWhenNothingRan(unittest.TestCase):
    def agent(self, tool_ids):
        return SimpleNamespace(_inbox=SimpleNamespace(local=SimpleNamespace(tool_ids=tool_ids)))

    def test_a_timeout_before_any_tool_hands_off(self):
        rejected = timed_out_before_acting(RuntimeError('Claude Code returned an error result: Request timed out'),
                                           self.agent(set()))
        self.assertIsInstance(rejected, Unavailable)

    def test_a_timeout_after_a_tool_ran_still_stops_the_turn(self):
        """Repeating a tool call on another route is not safe; that exclusion stands."""
        self.assertIsNone(timed_out_before_acting(RuntimeError('Request timed out'), self.agent({'toolu_1'})))

    def test_other_errors_are_unchanged(self):
        self.assertIsNone(timed_out_before_acting(RuntimeError('context length exceeded'), self.agent(set())))
        self.assertIsNone(failure(RuntimeError('Request timed out')))   # the classifier itself is untouched

class TheStoppedNoteSpeaksHisLanguage(unittest.TestCase):
    """2026-11-19: a Chinese conversation got the English crash line and read as the agent ignoring him."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from tests.agent_harness import make_agent
        self.agent = make_agent('coordinator', Path(self.tmp.name))

    def test_chinese_in_chinese_english_in_english(self):
        chinese = [{'role': 'user', 'content': '收盘前的被动资金流怎么算'}]
        self.assertIn('抱歉', self.agent._stopped_note(chinese))
        english = [{'role': 'user', 'content': 'How is the passive flow computed?'}]
        self.assertIn('Sorry', self.agent._stopped_note(english))

    def test_it_reads_the_last_user_turn_and_survives_block_content(self):
        mixed = [{'role': 'user', 'content': 'hello'},
                 {'role': 'assistant', 'content': 'hi'},
                 {'role': 'user', 'content': [{'type': 'text', 'text': '期货领先现货有成熟的交易策略吗'}]}]
        self.assertIn('抱歉', self.agent._stopped_note(mixed))
        self.assertIn('Sorry', self.agent._stopped_note([]))


if __name__ == '__main__':
    unittest.main()
