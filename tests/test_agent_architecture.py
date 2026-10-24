"""Behavioral regressions for runtime isolation, current memory and approved learning."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'cato')]
from agent_core.memory_context import WorkingMemory,current_context,read_view
from agent_core.memory_store import reconcile
import engine

class Architecture(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.memory=self.root/'memory.md'
        self.original='''# Memory
## Owner
Alex
## Open Threads (legacy)
- [P] [important] next step (2026-10-09)
- NOT TRACKING (Alex): dropped
## Recent Activity
- 2026-10-09: retained event
<!-- MEMORY-DETAIL-BELOW -->
## History
Keep this exactly.
'''
        self.memory.write_text(self.original,encoding='utf-8')
    def tearDown(self): self.temp.cleanup()
    def test_runtime_state_is_not_shared(self):
        a,b=engine.AgentRuntime(),engine.AgentRuntime()
        a.conversations[1]=['a']; a._repeat_calls[1]=['x']
        self.assertEqual(b.conversations,{}); self.assertEqual(b._repeat_calls,{})
        self.assertIsNot(a._draining,b._draining)
        self.assertIs(a.run_turn.__self__,a)
    def test_threads_and_activity_have_one_current_source_and_preserve_original(self):
        state=WorkingMemory(self.root)
        self.assertIn('next step',state.render())
        state.thread('important','new next step')
        self.assertIn('NOT TRACKING',state.thread('dropped','oops'))
        state.thread('done','complete');state.thread('done',done=True)
        for i in range(7): state.activity('event '+str(i))
        self.assertEqual(self.memory.read_text(encoding='utf-8'),self.original)
        view=read_view(self.root)
        self.assertEqual(view.count('[important]'),1)
        self.assertIn('new next step',view)
        self.assertNotIn('[done]',current_context(self.root))
        self.assertIn('event 0',view)
        self.assertNotIn('event 0',current_context(self.root))
        self.assertTrue(state.snapshot()['threads'][0]['pinned'])
        state.unpin('important')
        self.assertFalse(state.snapshot()['threads'][0]['pinned'])
        # An old Markdown snapshot cannot overwrite newer DB state at the next startup.
        self.assertIn('new next step',WorkingMemory(self.root).render())
    def test_all_pinned_rejection_does_not_lose_state(self):
        state=WorkingMemory(self.root)
        for i in range(9): state.thread(str(i),'next',pinned=True)
        before=state.snapshot()
        self.assertIn('pinned',state.thread('overflow','next'))
        self.assertEqual(before,state.snapshot())
    def test_exact_correction_retains_history_and_rejects_stale_writer(self):
        self.assertIn('updated',reconcile(self.memory,'## Owner\nAlex','## Owner\nAlex, Paris'))
        self.assertIn('Nothing written',reconcile(self.memory,'not current','replacement'))
        self.assertIn('Keep this exactly.',self.memory.read_text(encoding='utf-8'))
        self.assertTrue(list((self.root/'memory_backups').glob('*.md')))


if __name__=='__main__': unittest.main()
