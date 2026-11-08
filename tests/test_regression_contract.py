"""Regression: a judge cannot silently omit a required item."""
import json
import unittest
from tabularium.learning import regression
class Coverage(unittest.TestCase):
    def test_a_partial_grade_is_not_a_pass(self):
        case = dict(request='Do A and B', plan=['A','B'], reference='A and B are required')
        judge = lambda p,s: json.dumps(dict(items=[dict(item='A',met=True)],substance=2,note='A checked'))
        self.assertFalse(regression.grade(judge,case,'A is done')['passed'])

    def test_string_true_is_not_a_boolean_pass(self):
        case = dict(request='Do A', plan=['A'], reference='A is required')
        judge = lambda p,s: json.dumps(dict(items=[dict(item='A',met='false')],substance=2,note=''))
        self.assertFalse(regression.grade(judge,case,'A is done')['passed'])
