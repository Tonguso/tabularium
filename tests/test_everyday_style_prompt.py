"""Installed communication cores stay separate from shared operating policy.
Actual prompt assembly and model requests are exercised by test_smoke_agents.
"""
from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[1]
class EverydayStyle(unittest.TestCase):
    def test_each_agent_retains_its_installed_communication_core(self):
        for agent in ('cato','tacitus','callimachus','vitruvius'):
            path=ROOT/agent/'skills/talk-like-a-human-being/runtime-core.md'
            text=path.read_text(encoding='utf-8')
            self.assertEqual(text.count('## Everyday communication core'),1)
            self.assertLess(len(text),2200)
            for word in ('uncertainty','actual status','full-width punctuation','on demand',"owner's name"):
                self.assertIn(word,text)
if __name__=='__main__': unittest.main()
