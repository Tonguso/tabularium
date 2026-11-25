"""Tools the model is told about in one line each and given in full only when it reaches for one (2026-11-25).

Every request carried every tool in full: 45 definitions and 56,000 characters for the coordinator, on each of about
25,000 model calls a month. In the thirty days to 2026-11-25 the 22 tools named here were 2.9% of the coordinator's
21,550 tool calls and 46% of that text. They are now one tool, use_tool, whose description lists them. The first call
for a tool in a turn returns its full description and input schema, exactly the text that used to be sent every time,
and runs nothing; the next call runs it. So the instructions for a tool arrive when it is about to be used, and a tool
that is not used costs one line.

What decides the list is arithmetic, not taste: a tool belongs here when its definition, read on every call, costs
more than one extra model call each time it is used. The runtime counts those extra calls (usage event
'tool_instructions'), so a tool that turns out to be used often shows up and moves back.

The dispatcher runs the named tool as itself (core/engine.execute_tool): approvals, the path boundary, action records
and the usage counts all see the real tool, never use_tool. A saved conversation keeps the call as the model made it;
called() reads either form.
"""
import json

USE_TOOL = "use_tool"

DEFERRED = {
    "lesson": "turn an observed failure into a change of method: diagnose, propose the edit, apply it on Alex's go, measure it",
    "desktop": "Alex's desktop: screen, mouse and keyboard, for what has no other way in",
    "send_email": "send an email from this agent's own mailbox",
    "read_email": "read this agent's own mailbox",
    "email_status": "recheck an email receipt without sending again",
    "browser_navigate": "open a URL in a headless browser and return the page text; the page stays open for the tools below",
    "browser_click": "click an element on the open browser page",
    "browser_type": "type into an input field on the open browser page",
    "browser_screenshot": "send Alex a screenshot of the open browser page",
    "browser_close": "close the browser",
    "generate_image": "generate a picture with an image model and send it to Alex",
    "edit_image": "edit a photo Alex has sent, keeping the actual photo",
    "list_images": "find saved original photos by caption or image ID",
    "speak": "send Alex a spoken voice message",
    "consult_advisor": "a second opinion from a stronger model before committing to something important",
    "specialist_review": "a stateless research, quant, critic or delivery review through the paid advisor",
    "review_output": "task-specific QA of a workbook, deck or research conclusion before delivery",
    "verify_artifact": "check that a saved file exists, its hash and that it opens",
    "capability_check": "inspect what this agent can reach (configuration, mailbox, a website) before a substantial task",
    "sec_filing": "SEC EDGAR: a company's recent filings, or one section of its latest 10-K or 10-Q",
    "list_research_reports": "scan dated research posts and their summaries across a date range",
    "read_research_reports": "deep-read selected research PDFs after Alex has chosen them",
}


def called(block):
    """The tool a saved tool_use block ran and the inputs it ran with: a use_tool block is the tool it names."""
    name, inputs = block.get("name"), block.get("input")
    if name == USE_TOOL and isinstance(inputs, dict) and isinstance(inputs.get("input"), dict):
        return inputs.get("name"), inputs["input"]
    return name, inputs


def fold(tools, shape=None):
    """The tool list a request carries: every tool not named in DEFERRED in full, then use_tool listing the rest.
    `shape` turns use_tool into the provider's form when the list is not in the Anthropic one."""
    name_of = lambda t: (t.get("function") or t)["name"]
    kept = [t for t in tools if name_of(t) not in DEFERRED]
    names = [name_of(t) for t in tools if name_of(t) in DEFERRED]
    if not names:
        return kept
    return kept + [(shape or dict)({
        "name": USE_TOOL,
        "description": "More tools, listed here in one line each. Wherever your instructions name one of them, this is how "
                       "it is called. Call use_tool with name alone to get that tool's full instructions and input schema; "
                       "nothing runs. Then call use_tool with name and input to run it (input {} for a tool that takes "
                       "none). The first call for a tool in a turn always returns its instructions.\n"
                       + "\n".join(f"- {name}: {DEFERRED[name]}" for name in names),
        "input_schema": {"type": "object", "properties": {
            "name": {"type": "string", "enum": names},
            "input": {"type": "object", "additionalProperties": True,
                      "description": "The named tool's own input, as its schema describes."}},
            "required": ["name"]}})]


def instructions(schema, ran):
    """What the first call for a tool returns: the definition the model would otherwise have read on every request."""
    return (f"{schema['name']}: {schema.get('description', '')}\n\nInput schema:\n"
            + json.dumps(schema.get("input_schema", {}), ensure_ascii=False)
            + ("\n\nNOT run: a tool's instructions come before its first use in a turn. Check your input against them, "
               "then call use_tool again with name and input." if ran else
               f"\n\nNothing has run. To run it: use_tool with name \"{schema['name']}\" and input."))
