"""
Cato -- Telegram Interface

Run:
    python D:\\agents\\cato\\telegram.py

The message path lives in agent_core/agent_app.py (one class for all four agents, 2026-10-11). This file holds what is
Cato's alone: his configuration, the delegate tool, lesson approval and video intake.
"""

# --- durable logging: tee console output to D:\\Agents\\logs\\ ---
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from agent_logging import start_logging as _start_logging
import os as _os
if not _os.environ.get("CATO_BACKGROUND_TASK"):
    _start_logging("cato")

import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

AGENT_DIR   = Path(__file__).resolve().parent          # e.g. D:\agents\cato

import video_intake as _video
import delegation_contract as _contract
from agent_core.agent_app import AgentApp, Config
from agent_core.roster import ROLES
from agent_core import verification

# Brief-quality rules (decompose, size, tier, know your delegate) live in delegation_contract.py so tests
# can import them; the judgment they cannot make is in the work-delegation skill and its references/.
_validate_items = _contract.validate_items

DELEGATE_TOOL = {"name": "delegate", "description": "Send focused work to Cal (research, evidence packs) or Vit (engineering) and track it to acceptance. Delegate when the work would bloat your own context (evidence gathering across many sources), can run in parallel with yours, needs a capability only they have, or needs an independent check; keep the judgment yourself. Before submit, load the work-delegation skill and read the delegate card (skills/work-delegation/references/delegates/<agent>.json). submit needs agent, brief, purpose, completion_items (numbered, checkable, critical first, item 1 the early judgment slice), completion_condition, budget_tokens (a target the delegate sizes by and reports against; never a stop), output_path inside the delegate's writable paths, and for Vit allowed_paths. result fetches the current delivery and shared agreement; accept reviews that exact delivery, with one verdict per item (delivery of your own task is blocked until every attached request is accepted or cancelled). rework resumes the delegate on named items; rescope updates checks and adds obligations without removing existing items; extend sets a new target; status while it runs returns its latest checkpoint. Never poll in a loop: finish independent work, checkpoint, end the turn; the runtime resumes you when the result returns.",
     "input_schema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["submit", "status", "result", "accept", "rework", "rescope", "extend", "cancel", "answer"]},
         "agent": {"type": "string", "enum": ["cal", "vit"]},
         "brief": {"type": "string", "description": "Self-contained brief: question, scope, inputs/paths."},
         "purpose": {"type": "string", "description": "Short label of what this request is for."},
         "task_type": {"type": "string", "enum": ["basket_build", "basket_verification", "research", "engineering", "probe", "other"], "description": "Optional; for the cost history. Defaults to other."},
         "completion_items": {"type": "array", "items": verification.ITEM, "description": "submit/rescope: numbered definition of done, critical first, at most 3 critical / 6 total (more = two requests), item 1 early:true."},
         "output_path": {"type": "string", "description": "submit: where the delegate saves its deliverable; must be inside its writable paths (see its card)."},
         "brief_lesson": {"type": "string", "description": "accept with a critical item not met: what the brief should have said or sized differently."},
         "completion_condition": {"type": "string", "description": "submit: one-line summary of done."},
         "budget_tokens": {"type": "integer", "minimum": 1, "description": "Effective-token target for this request (submit), or a new target (extend/rework/rescope). Information for the delegate, never a stop."},
         "guesses_verdict": {"type": "string", "description": "accept: verdict on the delegate's stated assumptions (its checkpoint guesses): 'ok' if each matches Alex's intent, or 'wrong: <which and what was meant>' (counts as a rework; rework the affected items)"},
         "verdicts": {"type": "array", "items": {"type": "object", "properties": {"item": {"type": "integer"}, "verdict": {"type": "string", "enum": ["met", "partial", "not_met", "unverified"]}, "evidence": {"type": "string"}, "how": {"type": "string", "enum": ["ran", "read", "report", "judgment"], "description": "ran: you executed, launched or opened the deliverable yourself; read: you inspected its content or output yourself; report: taken from the delegate's report only. For executable checks, proof is derived from verify_requirement. For subjective items, read/judgment records your assessment; report alone cannot establish success. Explicit rejection remains open."}}, "required": ["item", "verdict", "evidence", "how"]}, "description": "accept: one per completion item; evidence names what was checked and how."},
         "items": {"type": "array", "items": {"type": "integer"}, "description": "rework: completion item numbers to redo."},
         "note": {"type": "string", "description": "rework/rescope: the gap or the reason, passed to the delegate; answer: the answer to the delegate's question."},
         "stop_rule": {"type": "string", "description": "Optional stop instruction in words (method and when to stop), e.g. 'one search pass per name; save filings locally and extract with a script; stop when no membership change is likely'. Not money: the target is budget_tokens."},
         "title": {"type": "string"}, "allowed_paths": {"type": "array", "items": {"type": "string"}},
         "delivery_id": {"type": "string", "description": "accept: exact delivery_id returned by result; binds this review to the delivery you saw."},
         "owner_tier": {"type": "string", "description": "submit: why Alex's yes is needed before the result is applied or merged (it touches his work computer, index calculation, reference data, or sends on his behalf). Leave empty for everything else: Cato applies or merges on green checks."},
         "request_id": {"type": "string"}, "task_id": {"type": "integer", "description": "Foreground only: an existing background task this request belongs to. Leave it out to hand work over on its own: the delegate starts now, the runtime creates the task that will receive and deliver the result, and you carry on. Never queue a background task of your own just to hand work over."}},
         "required": ["action", "agent"]}}


CONFIG = Config(
    name="cato", role=ROLES["cato"], display="Cato",
    token_env="TELEGRAM_BOT_TOKEN", token_missing="TELEGRAM_BOT_TOKEN not set.",
    provider_env="MODEL_PROVIDER", provider_default="anthropic",
    instance_port=8212,        # cato=8212, tacitus=8213, callimachus=8215, vitruvius=8216, watchdog=8211
    cdp_port=9222,
    openai_commands=("openai",),
    xlsx_uploads=True, document_types=".xlsx, .docx, .txt, or .pdf",
    voice_env="CATO_ELEVENLABS_VOICE_ID",
    background_refusals=True,          # workers: no polling other agents' task_state, no delivery before delegations are read
    tools_after=(("read_research_reports", DELEGATE_TOOL),),
    telegram_prompt="""

## Telegram Formatting

You are running inside a Telegram bot. Keep responses concise and conversational.
- Write mostly in plain prose. Use **bold** or *italics* sparingly — only to emphasise a genuinely
  key term or number. Most messages need none. Do not decorate or emphasise out of habit.
- No headers, horizontal rules, or tables.
- For lists, use plain numbering (1. 2. 3.) or write inline.
- Telegram has no math rendering: write formulas in plain text / Unicode (Σ, ×, ÷, √, subscripts
  like shares_pr). Underscores in notation render literally, so use them freely for variables.

## Email
Use POLICY.md for authorization; list available sender accounts when needed.
""",
    character_prompt="""- Convey the JARVIS qualities Alex requested: composed, capable, discreet, concise and quietly
  warm. Use "sir" naturally in occasional acknowledgements or direct advice, including mixed-language
  exchanges when it fits. It is his preferred form of address, not flattery or filler; do not put it
  into every reply, quoted material, reports or emails. Never translate his name to a Chinese name.
- Exercise independent judgment and occasionally understated dry humour when the mood permits.
  During a failure or frustration, prioritise facts and recovery, not jokes or theatrical apologies.
  Keep the existing bilingual judgment and voice profiles. Do not invent progress to sound competent.
""",
    ops_slots=dict(
        recurring="""- For recurring tasks, look up project_record(kind=workflow). Follow the scoped prerequisites,
  inputs, steps, checks and failure recovery plus the appropriate existing skill. Record successful
  run evidence before claiming a workflow validated. Preview/diff consequential changes; existing
  user authorization still applies to routine work. Do not learn broad procedures from one success.""",
        advisor="  Alex authorized relevant private/work evidence to the paid Anthropic advisor API, not credentials.",
        records="""- Use project_record for ongoing projects and maintained research theses. Check existing keys first;
  preserve the actual scope, approved decisions, dependencies, next action, completion criteria and
  artifact/evidence links. Separate thesis support from counterevidence and invalidation conditions.
  Retrieve only relevant records; do not dump the register into every prompt."""),
    worker_note="\nYou are the background worker. Execute the assigned task here; do not start or control other background tasks. Use the delegate tool for Cal/Vit work where it adds something distinct, and read every attached delegation result before delivering. Preserve checkpoints and end with findings, artifacts, evidence and any remaining work.",
    core_memory_detail="(project specifics like current methodologies, tools, infrastructure)",
    help_text=(
        "/tasks — background status\n"
        "/background <task> — start background work\n"
        "/steer \"task name\" <instruction> · /cancel <task name> · /resume <task name>\n"
        "/start   — reset conversation, reload memory\n"
        "/clear   — clear conversation (memory intact)\n"
        "/reload  — reload memory into current session\n"
        "/memory  — view current memory file\n"
        "/model   — show current model\n"
        "/model <name> — switch model\n"
        "           haiku · sonnet · opus · fable\n"
        "           opus 4.8 · opus 4.7 · sonnet 4.6 · etc.\n"
        "           openai · local\n"
        "/voice   — voice replies: on · off · auto\n"
        "/auto    — on: run everything · off: confirm only risky commands (default)\n"
        "/openai  - subscription first, GPT-6.1 API fallback\n"
        "Voice    - send a voice note (transcribed via Whisper)\n"
        "Photo    — send an image for Cato to analyse\n"
        "File     — send .xlsx / .docx / .txt / .pdf"
    ),
    checkpoint_note=("\n\n[Silent checkpoint — if the recent conversation covered anything worth "
                     "remembering (a decision, a result, an open thread), call log_activity now "
                     "with one concise line. If nothing notable, ignore this. Either way, do NOT "
                     "mention this checkpoint, do not say you logged anything, and do not let it "
                     "change your reply — just answer Alex normally.]"),
    nightly_save_prompt=(
        "It is now time for the nightly save. Review this entire conversation and save everything "
        "worth remembering to your memory file — decisions made, important context, anything "
        "Alex would want you to recall in the next session. Be thorough."
    ),
)


class CatoApp(AgentApp):
    def __init__(self, config, agent_dir):
        self.DELEGATE_SCRIPT = agent_dir / "scripts" / "delegate_local.py"
        super().__init__(config, agent_dir)

    def dispatch_extra(self, name, inputs, chat_id):
        if name == "delegate":
            return self.tool_delegate(chat_id=chat_id, **inputs)
        return None

    def tool_delegate(self, action: str, chat_id: int, agent: str = "", brief: str = "", purpose: str = "",
                      completion_condition: str = "", stop_rule: str = "", budget: str = "", title: str = "", allowed_paths=None,
                      request_id: str = "", task_id: int = 0, budget_tokens: int = 0, owner_tier: str = "",
                      task_type: str = "", completion_items=None, size_reason: str = "", verdicts=None, items=None, guesses_verdict: str = "",
                      note: str = "", sizing=None, output_path: str = "", override_reason: str = "", brief_lesson: str = "",
                      delegation_reason: str = "", reason_detail: str = "", direct_cost_tokens: int = 0,
                      fields=None, delivery_id="", _runner=None) -> str:
        """Wrap scripts/delegate_local.py (its credential checks and audit) and keep the per-task register."""
        stop_rule = stop_rule or budget          # 'budget' was the old name of the free-text stop rule (renamed 2026-10-08)
        if agent not in ("cal", "vit"):
            return "Use agent 'cal' or 'vit'."
        if action not in ("submit", "status", "result", "accept", "rework", "rescope", "extend", "cancel", "answer"):
            return "Use action submit, status, result, accept, rework, rescope, extend, cancel or answer."
        own = int(self.BACKGROUND_TASK_ID) if self.BACKGROUND_TASK_ID else 0
        if own and task_id and int(task_id) != own:
            return "A worker can only attach delegations to its own task."
        target = own or int(task_id or 0)
        if target and self._background.get(target)["chat"] != chat_id:
            return "That task belongs to another chat."
        temp = None
        if action == "accept":
            # Local: a result counts as read only after a verdict per completion item, each with evidence.
            if not request_id:
                return "accept needs request_id."
            try:
                contract_items = self._background.contract_items(request_id)
            except ValueError as exc:
                return str(exc)
            error = _contract.guesses_refusal(self._background.guesses(request_id), guesses_verdict)
            if error:
                return error
            report_only = [v.get('item') for v in (verdicts or []) if isinstance(v, dict) and v.get('how') == 'report']
            if report_only and not verification.cold_review(self.AGENT_DIR, delivery_id):
                return (f"Items {report_only} rest on the delegate's word. Get a cold read of this delivery first: "
                        f"specialist_review(role='delivery', delivery_id='{delivery_id}', question=<the completion condition>, "
                        "evidence=<the result text and the numbered completion items>), then accept.")
            if guesses_verdict:
                self._background.record_guesses_verdict(request_id, guesses_verdict.strip())
                if guesses_verdict.strip().lower().startswith("wrong:"):
                    self._background.bump_rework(request_id)        # a wrong assumption is a brief defect, counted like a rework
            idents = [target] if target else self._background.delegation_tasks(request_id)
            critical_unmet, unmet = [], []
            try:
                for ident in idents or [None]:
                    critical_unmet, unmet = self._background.accept_delegation(ident, request_id, verdicts, delivery_id=delivery_id)
            except ValueError as exc:
                return str(exc)
            card = self._background.scorecard(request_id)
            verdicts = json.loads(card['verdicts'])
            provisional_unmet = [v['item'] for v in verdicts if v['verdict'] == 'not_met' and contract_items[v['item']-1].get('critical')]
            if provisional_unmet and len(brief_lesson.strip()) < 20:
                return f"Review recorded as rejected on items {provisional_unmet}. Add brief_lesson naming how the brief should improve, then request rework."
            if provisional_unmet:
                self._background.record_lesson(request_id, brief_lesson.strip())
                _contract.append_lesson(request_id, (card or {}).get("agent", agent), brief_lesson, critical_unmet)
                card = self._background.scorecard(request_id)
            elif not unmet and card and not card.get("reworks"):
                _contract.save_exemplar(card, "accepted")       # first pass, all critical met: a worked example for next time
            if critical_unmet:
                guidance = (f"Critical items failed or unverified: {critical_unmet}. Either rework (request_id, items={critical_unmet}, note=<the gap>) "
                            "so the delegate resumes on those alone, record the unresolved gap and keep this delivery open. "
                            "Do not present this result as complete.")
            elif unmet:
                guidance = f"Non-critical items {unmet} remain open; request targeted rework. This delivery is not fully accepted."
            else:
                guidance = "All items met. Report the scorecard line to Alex: asked, delivered, verified how, cost."
                tier = (json.loads(card.get('brief') or '{}') if card and (card.get('brief') or '').startswith('{') else {}).get('owner_tier')
                if tier:
                    # Cato's accept is never Alex's approval (2026-10-27): an owner-tier result waits on his yes.
                    question = f"Request {request_id} is accepted by Cato and is owner tier ({tier}). Apply or merge it only on your yes."
                    try:
                        decision = self._background.ask(chat_id, idents[0], question) if idents else None
                        why = "no owner task to hold the decision"
                    except ValueError as exc:
                        decision, why = None, str(exc)
                    guidance = ("All items met. Owner tier: " + (f"decision #{decision['id']} is pending; deliver the result with that "
                                "question and apply or merge nothing until Alex says yes." if decision else f"ask Alex before anything is applied or merged ({why})."))
            return json.dumps({"accepted": not unmet, "verified": not unmet and all(not verification.is_judgment(it) for it in contract_items), "critical_unmet": critical_unmet, "unmet": unmet, "scorecard": {k: card.get(k) for k in ("items_total", "items_met", "critical_total", "critical_met", "budget", "spent", "reworks", "verdicts")}, "next": guidance}, ensure_ascii=False)
        if action == "submit":
            if not brief.strip() or not purpose.strip():
                return "submit needs a self-contained brief and a short purpose."
            # Definition of done and a target number are what past jobs lacked: the MAST failure study puts bad
            # specification and missing verification ahead of model quality. No justification form (Alex, 2026-10-08).
            task_type = task_type if task_type in self._task_cost.TASK_TYPES else "other"
            error = _validate_items(completion_items)
            if error:
                return error
            if not completion_condition.strip():
                return "submit needs completion_condition: a one-line summary of done."
            import background_runtime as _rt
            bad = _rt.unquoted_owner_lines(brief)
            if bad:
                return _rt.DECISION_QUOTE_RULE + " Offending brief lines: " + bad
            if not isinstance(budget_tokens, int) or isinstance(budget_tokens, bool) or budget_tokens <= 0:
                return "submit needs budget_tokens (effective tokens, by task type from cato/reports/task_cost_history_*.md)."
            card = _contract.load_card(agent)
            error = _contract.path_refusal(agent, card, output_path, brief, json.dumps(completion_items, ensure_ascii=False))
            if error:
                return error
            import uuid
            request_id = request_id or f"cato-{agent}-{target or 'fg'}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
            numbered = "\n".join(f"{n}. {'[critical] ' if it['critical'] else ''}{it['item']} (check: {it['check']})" for n, it in enumerate(completion_items, 1))
            output = completion_condition + "\nCompletion items, critical first:\n" + numbered
            request = {"request_id": request_id, "title": " ".join((title or purpose).split())[:90],
                       "question": brief, "scope": brief + (f"\nStop rule: {stop_rule}" if stop_rule else ""),
                       "privacy_constraints": "No credentials or secrets. Use private/work material only as the brief requires.",
                       "output_requirements": output, "completion_condition": completion_condition,
                       "completion_items": completion_items, "task_type": task_type, "sizing": sizing,
                       "delegation_reason": delegation_reason, "reason_detail": reason_detail, "direct_cost_tokens": int(direct_cost_tokens or 0),
                       "budget_effective_tokens": int(budget_tokens)}
            if output_path:
                request["output_path"] = output_path
            if owner_tier.strip():
                request["owner_tier"] = owner_tier.strip()
            if card.get("writable_paths"):
                request["writable_paths"] = card["writable_paths"]
            if agent == "cal":
                request.update(depth=stop_rule or "Proportionate to the brief; stop when the completion condition is met.",
                               permitted_sources="Public web sources and local files named in the brief.")
            else:
                if not allowed_paths:
                    return "Vit submit needs allowed_paths (scoped absolute paths)."
                request.update(allowed_paths=list(allowed_paths), acceptance_criteria=output)
            request.update(fields or {})
            temp = self.RUNTIME_DIR / "scratch" / f"delegate_{request_id}.json"
            temp.parent.mkdir(parents=True, exist_ok=True)
            temp.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
            args = ["--request-file", str(temp)]
        elif action == "answer":
            if not request_id or not note.strip():
                return "answer needs request_id and note: the answer to the delegate's question."
            request = {"request_id": request_id, "note": note}
            temp = self.RUNTIME_DIR / "scratch" / f"delegate_answer_{request_id}.json"
            temp.parent.mkdir(parents=True, exist_ok=True)
            temp.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
            args = ["--request-file", str(temp)]
        elif action in ("extend", "rework", "rescope"):
            if not request_id:
                return f"{action} needs request_id."
            has_budget = isinstance(budget_tokens, int) and not isinstance(budget_tokens, bool) and budget_tokens > 0
            if action == "extend" and not has_budget:
                return "extend needs the new budget_tokens."
            if action == "rework" and not (isinstance(items, list) and items and all(isinstance(i, int) for i in items)):
                return "rework needs items: the completion item numbers to redo, plus a note naming the gap."
            snapshot = {}
            if action == "rework":
                owner = target or next(iter(self._background.delegation_tasks(request_id)), 0)
                refusal = verification.rework_refusal(self._background, owner, request_id, items)
                if refusal:
                    return refusal
                snapshot = verification.rework_snapshot(self._background, owner, request_id, items) if owner else {}
            if action == "rescope":
                try:
                    verification.validate_items(completion_items)
                except ValueError as exc:
                    return str(exc)
            request = {"request_id": request_id, "note": note}
            if has_budget:
                request["budget_effective_tokens"] = int(budget_tokens)
            if action == "rework":
                request["items"] = items
            if action == "rescope":
                request["completion_items"] = completion_items
            temp = self.RUNTIME_DIR / "scratch" / f"delegate_{action}_{request_id}.json"
            temp.parent.mkdir(parents=True, exist_ok=True)
            temp.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
            args = ["--request-file", str(temp)]
        else:
            if not request_id:
                return f"{action} needs request_id."
            if own and action == "status":
                refusal = self._background.status_refusal(own, request_id)
                if refusal:
                    return refusal
            if action == "cancel" and own and any(d["request_id"] == request_id and d["source"] == "owner_decision"
                                                  for d in self._background.delegations(own)):
                return ("This request carries out Alex's decision; a worker cannot cancel it. Read its result, or report "
                        "why it cannot be used with a 'NOT DONE: Alex decided <X>; not carried out because <Y>' line.")
            args = ["--request-id", request_id]
        try:
            run = _runner or (lambda argv: subprocess.run(argv, capture_output=True, encoding="utf-8", errors="replace",
                                                           timeout=58, env=dict(os.environ, PYTHONIOENCODING="utf-8")))
            proc = run([sys.executable, str(self.DELEGATE_SCRIPT), agent, action, *args])
        except subprocess.TimeoutExpired:
            return f"delegate {action} timed out; remote outcome UNKNOWN. Check status for {request_id} before retrying."
        finally:
            if temp:
                temp.unlink(missing_ok=True)
        try:
            response = json.loads(proc.stdout)
        except ValueError:
            response = None
        if proc.returncode != 0 or not isinstance(response, dict):
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
            return f"delegate {action} failed (exit {proc.returncode}); nothing registered. " + " | ".join(tail)
        if action == "submit" and not target and not own:
            # The foreground handed work over: the delegate is already running. The owner that will receive, accept
            # and deliver its result is born parked, so the hand-off holds no worker slot (2026-10-21).
            who = "Cal" if agent == "cal" else "Vit"
            owner = self._background.start(
                chat_id, f"Receive {who}'s work for Alex: {' '.join((title or purpose).split())[:120]} (request {request_id}). "
                         f"When it returns, read it with delegate(action='result', agent='{agent}', request_id='{request_id}'), "
                         "accept it against the completion items, and deliver the outcome to Alex.",
                context="Brief sent to the delegate:\n" + brief, extra={"delegation_owner": request_id},
                parked_on={"agent": agent, "request_id": request_id, "purpose": purpose})
            target, handed_off = owner["id"], True
            response["owner_task"] = owner["summary"]
        else:
            handed_off = False
        tasks = [target] if target else self._background.delegation_tasks(request_id)
        if action == "submit":
            self._background.record_submission(request_id, target, agent, task_type, budget_tokens, completion_items, brief=request,
                                               reason=delegation_reason, direct_cost=int(direct_cost_tokens or 0))
        elif action == "rework":
            self._background.bump_rework(request_id, sent=items, snapshot=snapshot)
        if action == "submit" and target:
            source = "worker_judgment" if own else "owner_decision"
            self._background.add_delegation(target, agent, request_id, purpose, source)
            if not own and not handed_off:
                steered = self._background.control(chat_id, target, "steer",
                    f"{'Cal' if agent == 'cal' else 'Vit'} request {request_id} in flight for {purpose} (dispatched by the foreground per Alex). "
                    f"You must read and use its result: delegate(action='result', agent='{agent}', request_id='{request_id}'). "
                    "Delivery is blocked until you do. If it genuinely cannot be used, your final message must include "
                    "'NOT DONE: Alex decided <X>; not carried out because <Y>'. Any further delegation that materially changes "
                    "scope or cost: report to the foreground instead of deciding.")
                if steered.get("warning"):
                    response["warning"] = steered["warning"]
        remote = response.get("status")
        for ident in tasks:
            if action == "cancel" and remote in ("cancelled", "cancelling", "timed_out"):
                self._background.mark_delegation(ident, request_id, "cancelled")
            elif action in ("extend", "rework", "rescope", "answer") and remote in ("queued", "starting", "running"):
                self._background.reopen_delegation(ident, request_id)
            elif remote in ("completed", "partial", "blocked", "timed_out", "cancelled", "capped", "infeasible"):
                self._background.mark_delegation(ident, request_id, "returned")
        if tasks:
            response["attached_delegations"] = {i: self._background.delegations(i) for i in tasks}
        if remote == "question":
            response["delegate_note"] = ("The delegate stopped on a question (see 'question'). Answer with delegate(action='answer', "
                                         "request_id, note=...); it resumes from its checkpoint. Nothing is spent while it waits.")
        if action in ("status", "result") and response.get("checkpoint"):
            try:
                stated = json.loads(response["checkpoint"]).get("guesses")
                if stated:
                    self._background.record_guesses(request_id, stated)
                    response["delegate_note_guesses"] = "The delegate stated assumptions (guesses); accept needs guesses_verdict 'ok' or 'wrong: ...'. " + "; ".join(str(g)[:120] for g in stated)
            except (ValueError, TypeError, AttributeError):
                pass
        if action == "result" and not response.get("text"):
            response["delegate_note"] = "No result text yet; this does not count as read. Poll status and fetch again."
        elif action == "result":
            try:
                for ident in tasks:
                    response["delivery_id"] = self._background.save_delegate_result(ident, request_id, response.get("text") or "")
            except (ValueError, OSError) as exc:
                return str(exc)
            owner_task = tasks[0] if tasks else 0
            remote_store, remote_task = verification.delegate(self._background, owner_task, request_id)
            response['contract'] = dict(response.get('contract') or {}, completion_items=self._background.contract_items(request_id),
                                        instructions=remote_store.scope_updates(remote_task))
            response["delegate_note"] = ("Fetched, NOT yet accepted. Verify each completion item (verify_artifact, review_output, recompute "
                                         "numbers, open sources; consult_advisor for judgment calls), then delegate(action='accept', "
                                         "delivery_id=<returned id>, verdicts=[{item, verdict, evidence, how}]) for every item. Delivery stays blocked until then.")
        if remote == "capped":
            response["delegate_note"] = ("Legacy status from before 2026-10-08 (a delegate paused at a cap). Read the partial result and "
                                         "checkpoint; rework/rescope on the open critical items, or accept the partial with verdicts.")
        elif remote == "infeasible":
            response["delegate_note"] = ("Legacy status from before 2026-10-08 (estimate above a cap). Rescope or resubmit; nothing stops a "
                                         "delegate for the number now.")
        return json.dumps(response, ensure_ascii=False)[:40000]

    # Videos (incl. round video notes, GIF animations and video files sent as documents). Registered
    # before handle_document so video documents land here. Runs in the inbox worker thread, never
    # the polling thread; video_intake bounds the whole job (~10 min) and every subprocess.
    def register_before_document(self):
        self.bot.message_handler(content_types=["video", "video_note", "animation", "document"],
                                 func=lambda m: _video.video_ref(m) is not None)(self._inbox.handler(self.handle_video))

    def handle_video(self, message):
        chat_id = message.chat.id
        self.ensure_initialised(chat_id)
        ref, kind = _video.video_ref(message)
        if _video.too_big(getattr(ref, "file_size", None)):
            self.send_message(chat_id, _video.TOO_BIG_TEXT)
            return
        self.send_message(chat_id, "Processing video…")
        try:
            path = _video.download(self.bot, ref, message.date)
            result = _video.process_video(path)
        except _video.TooBig:
            self.send_message(chat_id, _video.TOO_BIG_TEXT)
            return
        except Exception as e:
            self.send_message(chat_id, f"Video processing failed: {e}. Nothing was passed to the conversation.")
            return
        text = _video.describe(result, message.caption or "")
        picks = [result["sheet"]] + [p for _, p in _video.pick_attachments(result["frames"])]
        images = [("image/jpeg", base64.standard_b64encode(Path(p).read_bytes()).decode("utf-8")) for p in picks]
        reply_note = self._reply_context(message, with_image=False)[0]
        self._run_image_turn(chat_id, images, reply_note + text, message.date)


runtime = CatoApp(CONFIG, AGENT_DIR)


def __getattr__(name):          # background_runtime and the tests address the agent through the module
    return getattr(runtime, name)


if __name__ == "__main__":
    runtime.main()
