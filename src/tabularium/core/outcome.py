"""Tool outcomes carry their kind in the type, not in their first words (2026-10-15).

The runaway stop used to decide that a tool call had failed by matching the result's opening against ten strings
("Error", "Refused", "NOT executed", ...). A refusal phrased differently counted as success, and a success that happened
to start with "Error" counted as failure. Now a tool that declines returns Refused(text) and one that breaks returns
Failed(text); both are plain strings on the wire and to the model, and engine.unproductive reads the type. A refusal
or error returned as a bare string is caught by tests/test_outcomes.py."""


class Refused(str):
    """The tool did not act; the text says why and what to do instead."""


class Failed(str):
    """The tool acted and failed; the text is the error."""
