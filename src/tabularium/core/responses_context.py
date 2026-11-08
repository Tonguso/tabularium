"""Lossless, turn-local continuation of OpenAI Responses, separate from portable saved history."""
from copy import deepcopy
import json


class ResponsesContext:
    def __init__(self):
        self.history = []
        self.items = []
        self.model = None

    def prepare(self, history, convert, model):
        if model == self.model and self.history and history[:len(self.history)] == self.history:
            return self.items + convert(history[len(self.history):])
        # A context cut, correction, or model switch starts a new native prefix.
        return convert(history)

    def remember(self, history, blocks, sent, output, model):
        self.history = deepcopy(history + [{"role": "assistant", "content": blocks}])
        def serial(item):
            return item.model_dump(exclude_none=True) if hasattr(item, "model_dump") else vars(item)
        self.items = deepcopy(sent) + json.loads(json.dumps(output, default=serial))
        self.model = model
