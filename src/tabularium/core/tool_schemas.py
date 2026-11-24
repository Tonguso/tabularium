"""The tools the agent offers the model: their schemas, which of them a turn may use, and the form each provider needs.
"""
from tabularium.memory.memory_context import memory_tool_schema
from tabularium.records import verification
from tabularium.tools.sec_edgar import SEC_FILING_TOOL
from tabularium.memory import archive_search


# One checkpoint tool for every agent (2026-10-11 evening, Alex: [quote removed]). Until then Cato, Cal/Vit and Tacitus
# carried three versions; Cal and Vit's still said a lesson is "appended to lessons.md", a file retired on 2026-10-10.
# The store (background_runtime.TaskStore.checkpoint) accepts every field for every agent.
CHECKPOINT_TOOL = {"name":"task_checkpoint","description":"Save progress needed for continuation: completed steps, next action, evidence and blockers. Plans and estimates are optional when useful; no extra checkpoint is needed solely to announce completion. Record material assumptions in guesses, or ask if they change scope. completion_items merges into the durable requirement list; omission never deletes. source=0 identifies the brief; source=N identifies added instruction N. Existing id updates its check; the next id adds an obligation. Declare deliverables and constraints, then verify failed or unverified items. Passed proof for unchanged requirements and files remains valid. Subjective items remain unverified. lesson records an observed method failure.",
      "input_schema":{"type":"object","properties":{"completion_items":{"type":"array","items":verification.ITEM},"completed":{"type":"string"},"next_action":{"type":"string"},"evidence":{"type":"array","items":{"type":"string"}},"blocker":{"type":"string"},"estimate_effective_tokens":{"type":"integer","minimum":1},"items_done":{"type":"array","items":{"type":"integer"}},"plan":{"type":"array","items":{"type":"string"},"description":"Own tasks: the steps, in order."},"estimate_turns":{"type":"integer","minimum":1,"description":"Own tasks: model turns the plan needs."},"done_when":{"type":"string","description":"Own tasks: the test that proves the job is finished."},"task_type":{"type":"string","enum":["probe","basket_verification","basket_build","engineering","research","other"]},"lesson":{"type":"string","description":"Observed failure and proposed correction; saved as a candidate, never automatically applied."},"facts":{"type":"array","items":{"type":"string"},"description":"Ledger: facts established with their source, replaced whole each time."},"guesses":{"type":"array","items":{"type":"string"},"description":"Ledger: working assumptions not yet verified."},"decisions":{"type":"array","items":{"type":"string"},"description":"Owner-labelled: 'Alex: \"his words\"' (quote required), 'Cato: ...' for an assumption, 'System: ...' for a runtime rule.","description":"Ledger: decisions taken, each prefixed Alex: or Cato: (or your own name); these go into every brief you write."}},"required":["completed","next_action","evidence"]}}

PROJECT_RECORD_TOOL = {"name":"project_record","description":"Persistent project/thesis/workflow register, plus the lesson register to read (lessons are recorded with remember kind=lesson). list/read/history/save. Read current version before saving; updates replace data and preserve history. Project data requires title,goal,next_action,evidence,completion_criteria; add decisions,dependencies,artifacts,blocker,deadline as needed. Thesis requires title,claim,support,counterevidence,invalidators,catalysts,exposures; evidence entries need sources/dates/limitations. Workflow requires title,prerequisites,inputs,steps,checks,failure_recovery,scope,validation; keep verified run evidence in validation and preview consequential changes. Lessons: read, list and history only. Deadlines must have timezone offset; local reminders run only for active/blocked records. Do not invent projects or monitoring topics.",
  "input_schema":{"type":"object","properties":{"action":{"type":"string","enum":["list","read","history","save"]},"kind":{"type":"string","enum":["project","thesis","lesson","workflow"]},"key":{"type":"string"},"version":{"type":"integer"},"data":{"type":"object","additionalProperties":True}},"required":["action"]}}

def openai_tool(t: dict) -> dict:
    return {"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                             "parameters": t.get("input_schema", {"type": "object", "properties": {}, "required": []})}}


class ToolSchemas:
    """Part of AgentApp (tabularium.core.agent_app): methods moved here unchanged on 2026-11-24."""

    def build_tools(self) -> list:
        cfg = self.config
        tools = [
            {
                "name": "web_fetch",
                "description": "Read a URL as visible text, with its source URL. For long pages, supply query to return relevant passages; full text is archived for search_archived/read_file.",
                "input_schema": {
                    "type": "object",
                    "properties": {"url": {"type": "string", "description": "Full URL to fetch (include https://)."},
                                   "query": {"type": "string", "description": "The specific facts or passage to find on a long page."}},
                    "required": ["url"],
                },
            },
            SEC_FILING_TOOL,
            {
                "name": "read_memory",
                "description": "Read memory selectively. No arguments lists headings. Select section or query; follow next_offset for more. Historical activity never overrides current instructions.",
                "input_schema": {"type": "object", "properties": {
                    "section": {"type": "string", "description": "Exact heading, unique heading fragment, or all."},
                    "query": {"type": "string", "description": "Literal case-insensitive search in selected section or whole file."},
                    "offset": {"type": "integer", "minimum": 0, "description": "Character offset in selected text/search results."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 24000, "description": "Page size; default 12000 characters."}
                }, "required": []},
            },
            {
                "name": "skill",
                "description": (
                    "Load one of your installed skills — the full methodology for a kind of task. The skill "
                    'names are indexed in your system prompt. Use name="" to discover descriptions, or an exact '
                    "name to load instructions. Call it BEFORE starting work whenever a task matches a "
                    "skill's description, rather than relying on a hazy memory of the procedure. Skills hold "
                    "reusable method; memory holds facts and state — they are different things."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"name": {"type": "string", "description": "Exact skill name from the Available Skills list."}},
                    "required": ["name"],
                },
            },
            {
                "name": "log_activity",
                "description": (
                    "Record a one-line summary of what THIS conversation covered into your Recent Activity "
                    "log — a short, always-loaded rolling record of roughly the last two weeks. Use it so you "
                    "remember what you and Alex did recently: decisions made, work completed, open threads. "
                    "Keep each entry to one concise line. Older entries are moved automatically into an "
                    "Activity Archive in your memory file (nothing is lost) — so if Alex asks about something "
                    "from weeks or months ago that isn't in Recent Activity, call read_memory and check the "
                    "Activity Archive. Use remember for durable facts worth keeping."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"note": {"type": "string", "description": "One concise line summarising what happened or was decided."}},
                    "required": ["note"],
                },
            },
            {
                "name": "read_file",
                "description": "Read an excerpt of a text file or archived tool output: 8,000 characters by default (next offset is reported), up to 36,000 with limit. Do not read whole files to compare, count or transform them; run a script (e.g. python -m tabularium.tools.textdiff) and read its output, then read_file only the excerpts it points to.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute path to the file."},
                        "offset": {"type": "integer", "minimum": 0, "description": "Character offset; default 0."},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 36000, "description": "Characters to return; default 36000."},
                    },
                    "required": ["path"],
                },
            },
            archive_search.SEARCH_ARCHIVED_TOOL,
            {
                "name": "write_file",
                "description": ("Create a file or overwrite it with the full content. For NEW files, or "
                                "small ones. To change an existing file larger than ~10 KB use edit_file: "
                                "rewriting a big file in one call exceeds the output limit and nothing "
                                "gets written."),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path":    {"type": "string", "description": "Absolute path to the file."},
                        "content": {"type": "string", "description": "Text content to write."},
                    },
                    "required": ["path", "content"],
                },
            },
            {
                "name": "edit_file",
                "description": ("Change part of an existing file by exact find-and-replace. old_string "
                                "must appear exactly once (include a few surrounding lines to make it "
                                "unique) unless replace_all is true. Make several small edits rather "
                                "than one giant one. This is THE way to modify large files such as HTML "
                                "mockups, scripts and documents."),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path":        {"type": "string", "description": "Absolute path to an existing file."},
                        "old_string":  {"type": "string", "description": "Exact text to find (whitespace-sensitive)."},
                        "new_string":  {"type": "string", "description": "Replacement text. Empty string deletes old_string."},
                        "replace_all": {"type": "boolean", "description": "Replace every occurrence. Default false."},
                    },
                    "required": ["path", "old_string", "new_string"],
                },
            },
            {
                "name": "execute_command",
                "description": "Execute a PowerShell command on Alex's machine. Returns exit status and output; verbose logs are archived with excerpts. Search/read the archive for full evidence, not a rerun. Always confirm before destructive operations.",
                "input_schema": {
                    "type": "object",
                    "properties": {"command": {"type": "string", "description": "PowerShell command to run."}},
                    "required": ["command"],
                },
            },
            {
                "name": "view_image",
                "description": (
                    "LOOK at an image file on this machine — a slide you rendered, a chart you made, a "
                    "screenshot, any local picture. Use it to check your own work before showing Alex: "
                    "render slides with scripts\\deck.py then view them. Without this you are guessing "
                    "about anything visual you produced."),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Full path to the image file (png, jpg, gif, webp)."},
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "send_photo",
                "description": ("Send an image to Alex on Telegram as a photo: a local file (path) or a URL. marks first draws "
                                "red boxes with labels on it and crop sends only that region, so a question about a shape goes "
                                "out as the picture with the candidates drawn on it and one plain question, never as a numbered "
                                "list of geometry (units, degrees). Coordinates are pixels of the original file."),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path":    {"type": "string", "description": "Local image file to send."},
                        "url":     {"type": "string", "description": "Direct URL of the image to download and send, if no path."},
                        "caption": {"type": "string", "description": "Optional caption to include with the photo."},
                        "marks":   {"type": "array", "description": "Boxes to draw: x, y, w, h in original pixels and a short label such as A or 'this piece'.",
                                    "items": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}, "w": {"type": "integer"},
                                                                               "h": {"type": "integer"}, "label": {"type": "string"}},
                                              "required": ["x", "y", "w", "h"]}},
                        "crop":    {"type": "array", "items": {"type": "integer"}, "minItems": 4, "maxItems": 4,
                                    "description": "x, y, w, h of the region to send, in original pixels, after marking."},
                    },
                },
            },
            {
                "name": "generate_image",
                "description": (
                    "Generate a picture with OpenAI's image model and send it to Alex on Telegram: illustrations, "
                    "photos, scenes, mockups. Logos, icons, diagrams and charts are drawn as code (SVG, then rendered) "
                    "by the matching skill, not here. The service refuses trademarked logos and characters; when it "
                    "blocks a request, do not resend the same idea: change approach or ask Alex. Write a rich, "
                    "descriptive prompt yourself — expand his request with subject, style, lighting and "
                    "composition; do not just echo his words. It follows detailed prompts well and can "
                    "render readable text in images. Takes roughly 15-30 seconds. Each image costs a few "
                    "cents, so generate deliberately rather than speculatively."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "prompt":      {"type": "string", "description": "Detailed description of the image to create."},
                        "orientation": {"type": "string", "description": "square (default), landscape, or portrait."},
                        "quality":     {"type": "string", "description": "low, medium (default), or high. Use high only when detail really matters — it costs more."},
                    },
                    "required": ["prompt"],
                },
            },
            {
                "name": "edit_image",
                "description": (
                    "EDIT a photo Alex has sent — the actual photo, not a redrawn imitation. Use this "
                    "whenever he asks you to change something about a picture he sent: a different "
                    "hairstyle, change the background, remove an object, restyle the clothing, retouch it. "
                    "It operates on his most recent photo, so it works right after he sends one. "
                    "Describe the CHANGE you want in the prompt (e.g. 'give him a short textured crop with "
                    "a natural hairline, keep the face and lighting unchanged'). "
                    "This is real image-to-image editing — do NOT tell Alex you can only generate a new "
                    "image from scratch; that is what generate_image is for."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "prompt":  {"type": "string", "description": "What to change about the photo, and what to keep unchanged."},
                        "quality": {"type": "string", "description": "low, medium (default), or high."},
                    },
                    "required": ["prompt"],
                },
            },
            {
                "name": "send_document",
                "description": (
                    "Send a local file to Alex on Telegram — a spreadsheet (.xlsx), PDF, Word doc, text, etc. "
                    "Use this whenever Alex asks you to send him a file you built or have on disk. "
                    "Pass the absolute path to the file."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path":    {"type": "string", "description": "Absolute path to the file to send (e.g. D:\\report.xlsx)."},
                        "caption": {"type": "string", "description": "Optional caption/message to include."},
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "speak",
                "description": (
                    "Send a spoken voice message to Alex on Telegram. Use this whenever Alex asks you "
                    "to say something as a voice message, reply by voice, or read something aloud — even "
                    "if he TYPED the request. The text you pass is exactly what gets spoken in your voice. "
                    "Keep any accompanying text reply brief, since the voice carries the message."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "The exact words to speak aloud."},
                    },
                    "required": ["text"],
                },
            },
            {
                "name": "consult_advisor",
                "description": (
                    "Get a second opinion from a stronger advisor model (always one tier above the "
                    "model you are running on: Sonnet gets Opus, Opus gets Fable) before you "
                    "commit to something important. Use it for high-stakes or complex reasoning you want "
                    "double-checked — financial or index logic, a corporate-action factor, a multi-step "
                    "plan, or an answer you're genuinely unsure about. Pass everything the advisor needs "
                    "in one message: the goal, your current plan or answer, and the specific question. "
                    "Then weigh the advice on its merits — it is a second opinion, not an order. Do NOT "
                    "use it for routine or trivial work; it is slower and costs more."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "request": {"type": "string", "description": "Full context for the advisor: the goal, your plan/work/answer, and exactly what to check."},
                    },
                    "required": ["request"],
                },
            },
            {
                "name": "list_research_reports",
                "description": (
                    "Scan dated posts and their visible research summaries across a 知识星球 date range, "
                    "without opening/downloading PDFs or using OCR. No five-report cap. Use this for broad "
                    "weekly/theme scans: inspect the summaries, discuss candidate themes with Alex, then "
                    "let him choose reports for deeper reading. Report scan coverage and unavailable/collapsed "
                    "summaries honestly. Retrieve any archived output sections before claiming a full review."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "date_from": {"type": "string", "description": "Start date YYYY-MM-DD (inclusive). Defaults to today."},
                        "date_to":   {"type": "string", "description": "End date YYYY-MM-DD (inclusive). Defaults to today."},
                        "group_url": {"type": "string", "description": "Optional full group URL. For 默认研报圈, omit this field; never pass its display name here."},
                    },
                    "required": [],
                },
            },
            {
                "name": "read_research_reports",
                "description": (
                    "Deep-read selected research PDFs only AFTER Alex chooses the reports or explicitly "
                    "requests a PDF deep dive. Supply a filename filter for that selection. A broad weekly "
                    "summary/thematic scan belongs to list_research_reports; never use PDF OCR as a substitute "
                    "for post summaries. With no filter this tool returns the summary scan without downloading PDFs. "
                    "All pages are extracted locally; inspect returned coverage and pages needing vision before claiming full understanding."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "date_from":   {"type": "string", "description": "Start date YYYY-MM-DD (inclusive). Defaults to today."},
                        "date_to":     {"type": "string", "description": "End date YYYY-MM-DD (inclusive). Defaults to today."},
                        "group_url":   {"type": "string", "description": "Optional full group URL. For 默认研报圈, omit this field; never pass its display name here."},
                        "filter":      {"type": "string", "description": "Only read reports whose filename contains this substring (e.g. a broker name or topic). Strongly recommended."},
                        "max_reports": {"type": "integer", "description": "PDF batch size for selected reports only; default 5. This does not limit the summary scan."},
                        "full": {"type": "boolean", "description": "Return the entire text of each report. Default false: only the first preview_pages pages come back (token cost control); the full text is saved to disk. Set true only when Alex asks for the whole report."},
                        "preview_pages": {"type": "integer", "description": "Pages to return when full is false. Default 3."},
                    },
                    "required": [],
                },
            },
            {"name": "background_task", "description": "Run research, long jobs or work spanning many steps in a separate worker so conversation stays responsive. Not for work you can finish in this turn: do that here, with your own tools. Supply a self-contained scope plus relevant context, paths and authorizations. For start, declare completion_items covering every requested deliverable and constraint; they are saved before the worker starts and cannot be dropped by its checkpoints. Refer to jobs by a short descriptive name in user-facing replies; numeric IDs are internal tool handles only. Start, status, steer, cancel or resume. status lists the last ten tasks one line each (state, short label, last update); task_evidence(id) gives one task's full brief, actions and result. One background job runs at once; others queue. Resume requires direction; never replay unknown external actions.",
             "input_schema": {"type": "object", "properties": {
                 "action": {"type": "string", "enum": ["start", "status", "steer", "cancel", "resume"]},
                 "task_id": {"type": "integer"}, "request": {"type": "string"},
                 "completion_items": {"type": "array", "items": verification.ITEM},
                 "context": {"type": "string"}}, "required": ["action"]}},
            {"name": "verify_artifact", "description": "Check a saved artifact exists, its hash and structural readability. Does NOT prove formula calculations, layout, or substantive correctness. Use before claiming a generated file is ready; render decks and inspect outputs separately.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
            {"name": "email_status", "description": "Recheck an email receipt against sender Sent Items without sending it again.",
             "input_schema": {"type": "object", "properties": {"receipt_id": {"type": "string"}}, "required": ["receipt_id"]}},
            {"name": "search_conversations",
             "description": "Retrieve small dated excerpts from retained conversations and Telegram requests. Use for 'yesterday', 'last Thursday', prior decisions or disagreements; search before claiming to remember. Historical assistant claims are NOT Alex's decisions. Local keyword/Chinese search; try synonyms when needed. No full history is added to the prompt.",
             "input_schema": {"type": "object", "properties": {
                "query": {"type": "string"}, "date_from": {"type": "string", "description":"YYYY-MM-DD inclusive"},
                "date_to": {"type": "string", "description":"YYYY-MM-DD inclusive"},
                "speaker": {"type":"string","enum":["","user","assistant"]},
                "limit": {"type":"integer"}, "offset":{"type":"integer"}}, "required":[]}},
            {"name":"read_conversation_excerpt",
             "description":"Read an archived message by ID returned from search_conversations. Supports paging; quote accurately and distinguish user decisions from assistant suggestions.",
             "input_schema":{"type":"object","properties":{"message_id":{"type":"integer"},"offset":{"type":"integer"},"limit":{"type":"integer"}},"required":["message_id"]}},
            {"name":"review_output",
             "description":"Task-specific QA before delivering a workbook, deck or research conclusion. Excel: optional recalculation of a COPY with macros/links disabled, errors, explicit totals and units. PPT: real PowerPoint overflow checks and slide renders, then YOU inspect images. Research: pass claims with saved source paths and quotes; tool checks evidence availability, not truth or entailment. Full report saved locally. Heavy work belongs in background.",
             "input_schema":{"type":"object","properties":{
                "path":{"type":"string"},
                "checks":{"type":"array","items":{"type":"object","properties":{
                    "sheet":{"type":"string"},"cell":{"type":"string"},
                    "expected":{"anyOf":[{"type":"number"},{"type":"string"},{"type":"boolean"},{"type":"null"}]},"components":{"type":"array","items":{"type":"string"}},
                    "tolerance":{"type":"number"},"unit_label_cell":{"type":"string"},"expected_unit":{"type":"string"}},
                    "required":["sheet","cell"]}},
                "claims":{"type":"array","items":{"type":"object","properties":{
                    "claim":{"type":"string"},"source_path":{"type":"string"},"source_url":{"type":"string"},
                    "evidence_quote":{"type":"string"},"published_at":{"type":"string"},
                    "kind":{"type":"string","enum":["fact","inference"]}},
                    "required":["claim","source_path","evidence_quote","published_at","kind"]}},
                "recalculate":{"type":"boolean"},"render":{"type":"boolean"},
                "date_from":{"type":"string"},"date_to":{"type":"string"}},
                "required":[]}},
            memory_tool_schema(),
            {"name":"task_evidence","description":"Inspect a background task's actual worker lease, saved result availability and action record paths. Use before claiming progress or completion. A returned tool result alone does not prove success; open relevant records and artifacts.",
             "input_schema":{"type":"object","properties":{"task_id":{"type":"integer"},"limit":{"type":"integer"}},"required":["task_id"]}},
            PROJECT_RECORD_TOOL,
            CHECKPOINT_TOOL,
            verification.TOOL,
            {"name":"capability_check","description":"Inspect local capability evidence before substantial tasks. overview reports configuration and recent model responses without paid requests; email reports whether this agent has a mailbox key, without sending; browser with url checks the real page using existing headless navigation. A loaded page is not proof of authentication or membership. PDF/voice need actual artifact/request verification.",
             "input_schema":{"type":"object","properties":{"probe":{"type":"string","enum":["overview","email","browser"]},"url":{"type":"string"}},"required":[]}},
            {"name":"specialist_review",
             "description":"Stateless research/quant/critic/delivery review through the paid Anthropic advisor API. role delivery is the cold read of a delegate's delivery before accept: pass delivery_id from result, the completion condition as question, and the result text with the numbered completion items as evidence; accept refuses report-only verdicts without it. Alex approved relevant private/work evidence transfer; never include credentials. No persona/history/tools inherited. Supply original question, dated evidence excerpts with IDs and contrary evidence. Max three calls per turn and 24k characters. Use selectively, in background for long tasks. Findings are advisory, not verified outcomes or user approval.",
             "input_schema":{"type":"object","properties":{
              "role":{"type":"string","enum":["research","quant","critic","delivery"]},
              "question":{"type":"string"},"evidence":{"type":"string"},
              "proposed_answer":{"type":"string"},"counterevidence":{"type":"string"},
              "delivery_id":{"type":"string","description":"delivery role: the delivery_id returned by delegate result."}
             },"required":["role","question","evidence"]}},
            {"name":"list_images",
             "description":"Find saved original photos by caption or image ID, newest first. They survive follow-ups, history trimming and restart. Returns paths for view_image; never ask Alex to resend an available original. Paging supports older images; include_views also lists locally viewed renders.",
             "input_schema":{"type":"object","properties":{
                "query":{"type":"string"},"limit":{"type":"integer"},"offset":{"type":"integer"},
                "include_views":{"type":"boolean"}},"required":[]}},
        ]
        for after, schema in self.extra_tools():
            tools.insert([t["name"] for t in tools].index(after) + 1, schema)
        tools = [t for t in tools if t["name"] not in cfg.tools_remove]
        if cfg.tool_allow:
            tools = [t for t in tools if t["name"] in cfg.tool_allow and t["name"] != "remember"] + [memory_tool_schema()]
        from tabularium.records.task_decisions import TOOL
        tools.append(TOOL)
        from tabularium.tools.desktop import TOOL as DESKTOP_TOOL      # every agent (Alex, 2026-11-07), gated by his yes
        tools.append(DESKTOP_TOOL)
        if cfg.role.learns and not cfg.tool_allow:
            from tabularium.learning.learning import TOOL as LESSON_TOOL
            tools.append(LESSON_TOOL)
        return tools

    def _preset_verification(self):
        return bool(self.BACKGROUND_TASK_ID and self._background.preset_case(int(self.BACKGROUND_TASK_ID)))

    def get_tools(self, provider: str) -> list:
        tools = self._all_tools(provider)
        if self._tool_scope is not None:
            tools = [t for t in tools if (t.get("function") or t).get("name") in self._tool_scope]
        if self._preset_verification():
            from copy import deepcopy
            tools = deepcopy(tools)
            tools = [t for t in tools if (t.get("function") or t).get("name") != "verify_requirement"]
            for tool in tools:
                body = tool.get("function") or tool
                if body.get("name") == "task_checkpoint":
                    body["description"] = "Save progress, evidence and blockers when useful for continuation. Acceptance checks are owned by the runtime."
                    schema = body.get("input_schema") or body["parameters"]
                    schema["properties"].pop("completion_items", None)
        return tools

    def _all_tools(self, provider: str) -> list:
        if not self.config.email_browser_tools:
            if provider == "anthropic":
                tools = [self.TAVILY_SEARCH_TOOL_ANTHROPIC] + self.BASE_TOOLS_ANTHROPIC
                tools[-1] = {**tools[-1], "cache_control": dict(self.CACHE_CONTROL)}
                return tools
            return [self.TAVILY_SEARCH_TOOL_OPENAI] + self.BASE_TOOLS_OPENAI
        email = self.EMAIL_TOOLS_ANTHROPIC if self.EMAIL_AVAILABLE else []
        email_oa = self.EMAIL_TOOLS_OPENAI if self.EMAIL_AVAILABLE else []
        browser = self.BROWSER_TOOLS_ANTHROPIC if self.PLAYWRIGHT_AVAILABLE else []
        browser_oa = self.BROWSER_TOOLS_OPENAI if self.PLAYWRIGHT_AVAILABLE else []
        if provider == "anthropic":
            search = self.TAVILY_SEARCH_TOOL_ANTHROPIC   # the agents' own web_search tool; the engine order decides where it goes
            tools = [search] + self.BASE_TOOLS_ANTHROPIC + email + browser
            # Cache the tools list — last tool gets the cache_control marker
            tools[-1] = {**tools[-1], "cache_control": dict(self.CACHE_CONTROL)}
            return tools
        return [self.TAVILY_SEARCH_TOOL_OPENAI] + self.BASE_TOOLS_OPENAI + email_oa + browser_oa
