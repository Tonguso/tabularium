---
name: work-delegation
description: Cost-aware work delegation and collaboration across any task. Load BEFORE collaborating with another agent, delegating to Vit or Cal, commissioning research or engineering, requesting an advisor or specialist review, or coordinating multiple workers. Applies regardless of domain alongside the relevant task skill. Enforces compact briefs, one focused challenge pass and decision-changing stopping gates.
metadata:
  author: Alex
  version: "1.3.6"
---

# Work delegation

## Scope
Load before any agent collaboration, delegation or external model review, regardless of task. Pair with domain skills; use vit-engineering for Vit implementation mechanics. This skill governs allocation and acceptance, not domain correctness. A background worker keeping conversation responsive is not automatically a reason to add more agents. Do not delegate just to appear thorough.

## Domain skills carry method only
Published skills (thematic-basket-construction and anything else in Alex's public repo) say how the work is done, never who does it or what it costs, so other people can use them without this setup. That part is this skill's job. Load this skill alongside a domain skill whenever the task could involve Cal, Vit, a research pack, a built artifact or a verifier, and decide here whether to collaborate: choose the executor by the standard under Coordination (best output quality, handover loss counted); delegate distinct questions or deliverables, not duplicate versions of the whole task. Alex can override the choice; routine delegation within existing authority needs no approval. Submit Cal/Vit assignments with the delegate tool, never hand-written request JSON or delegate_local.py. For a basket the usual split is: Cal gathers evidence per tier or facet, Vit builds the workbook when one is asked for, a clean verifier checks the deliverable against the skill's criteria; choose each piece by the same standard; backtests and basket price history only when Alex asked in the current request; co-movement at your judgment.

## Ownership and right of access
Cato is the default coordinator responsible for selecting agents, assignments, scope, cost, blockers and final acceptance. Alex may directly contact or instruct any agent at any time without going through Cato. This is not an exclusive gateway and does not transfer routine management to Alex. Use available shared records to reconcile direct instructions; never claim visibility of private exchanges. If instructions conflict or authorization is unclear, ask Alex with the conflict, options and a recommendation rather than silently overriding him.

For each deliverable name one execution owner. Cato manages outcomes and removes blockers, not a second step-by-step implementation chain. Approved routine reversible work proceeds without repeated approval. Escalate consequential trade-offs, scope or acceptance changes, new commitments, destructive actions and genuine uncertainty with a concise decision request. Do not invent extra approval requirements for ordinary implementation details.

## Proportional management
There is no default executor. For each piece of work, choose who does it (me, Cal or Vit) by which choice gives the best output quality: who holds the method or skill it needs, who has the capability, whether a fresh context or a different model improves it, and whether the handover would lose more than it gains. Whoever executes, I own the brief, steering, acceptance, rework and the report to Alex. Do not presume multiple agents improve quality. Use one short assignment; do not repeat the implementer's discovery or load implementation material solely to coordinate. Give each role only necessary context and skills while preserving mandatory instructions and domain checks. Reuse evidence by path and concise excerpts rather than repeatedly expanding tool output.

Before lengthy preparation, get a bounded assignment to the execution owner. Assess progress at the first inspectable output and final acceptance; add intermediate checks only for a real risk or blocker. Avoid polling merely for reassurance. Update Alex on actual milestones, material blockers or approaching investment limits, with evidence and the decision needed. A live worker, checkpoint or status message is not a deliverable.

Include coordination, implementation and acceptance in cost planning. Inspect their separate usage when available so coordination cannot invisibly consume the intended implementation effort. No universal overhead percentage is asserted. If preparation or coordination is consuming the allowance without inspectable output, stop expanding it, identify the blocker and narrow the work or escalate; do not automatically buy more budget. POLICY.md owns budget authority: estimates are reporting targets, never automatic caps; count coordination and implementation together. Optimize reliable useful output, not minimum tokens alone, and preserve mandatory QA.

## Before delegating
For short, bounded maintenance, work directly in the foreground. Metadata edits, copying a known file, diff checks, commits and pushes do not warrant a worker merely because they involve several commands. Judge actual workload and expected blocking time, not step count. Reserve background workers for genuinely lengthy research, large artifacts or slow computation; do not assign an oversized token allowance to routine work.

1. Name the decision or deliverable and the specific uncertainty or bottleneck the delegate can resolve. If no delegate improves the output once handover loss is counted, I execute it.
2. Inspect relevant capability evidence and existing active work. Reuse saved evidence; do not commission duplicates. Use existing authorized audited connections, not new infrastructure. Route all Cato-to-Vit/Cal operations through the delegate tool (action submit/status/result/cancel; submit needs agent, brief, purpose, plus allowed_paths for Vit). It builds the request, wraps the audited bridge and records the request against the task. Do not hand-write request JSON or call delegate_local.py directly; that bypasses the task register and the delivery gate. Keep the returned request_id and inspect unknown outcomes (timeouts) with status before any retry.
3. Every delegate submission carries a numeric budget_tokens target (EFFECTIVE tokens: uncached input ×1, cache write ×1.25, cached re-read ×0.1, output ×5; sized by task type and route from the latest cato/reports/task_cost_history_*.md) and a completion_condition. The target is information for the delegate, never a stop (see Budgets). Treat subscription quota and elapsed time as costs too. Do not change provider or incur new paid commitments without applicable authorization.
4. Set a bounded assignment and stopping condition. Give only necessary context, relevant source excerpts and paths. No full history, unrelated memory or credentials. Distinguish verified facts from hypotheses.

## Translating Alex's intent into briefs
A main reason for Cato to coordinate is that Cato turns Alex's requirements into briefs for other agents. Alex endorsed this on 2026-10-05: humans are generally worse at writing agent prompts. Cato holds context a fresh prompt lacks, such as standing preferences, prior decisions, approved templates, skill rules and earlier failures. Put the parts that matter into each brief. Do not make Alex restate them, and do not make the delegate rediscover them.

The risk runs the other way too. If Cato misreads Alex, every brief carries the mistake, and the delegate acts on it with confidence. Example from 2026-10-05: "keep your way" meant approval of Cato's general judgment, but it was nearly sent on as a cancellation of the split Alex had chosen. So:
- Before writing a brief, restate Alex's actual instruction to yourself. Separate what he decided from what he only commented on or approved in general.
- If a requirement is ambiguous and the answer would change the brief, ask Alex first. Asking after delegation means unwinding work.
- Pass on Alex's decisions as decisions and Cato's assumptions as assumptions, labelled.
- Keep briefs on disk and show Alex the exact text when he asks.

## Carrying out Alex's delegation decisions
When Alex decides mid-task that a part goes to Cal or Vit, the foreground submits it immediately with delegate(action='submit', ..., task_id=<background task>). Never pass the decision on by steer alone: a steer is advice the worker can drop, while an attached request is enforced. The attached request is recorded as Alex's decision, the worker gets a scope update, and the worker cannot cancel it.

A background worker may delegate on its own judgment when the contribution is distinct and useful. If that delegation would materially change scope or cost, report it to the foreground with a recommendation instead of deciding it.

## Talking to a delegate while it works
- **Progress.** `status` returns the delegate's latest checkpoint (completed, next_action, items_done) while it runs. Read it at the early judgment slice; rescope or rework then, not after a finished result you cannot use.
- **Questions.** A delegate that hits something it cannot decide (an ambiguity that changes the result, a missing input, an unusable path) stops with a QUESTION in its checkpoint. The runtime relays it to your task as a scope update and wakes you if you were parked. Answer with `delegate(action='answer', request_id, note=...)`; it resumes from its checkpoint and nothing is spent while it waits. Answer what you know; if it is Alex's decision, ask him and pass his answer on as his.
- **Results.** Every result opens with DONE / OPEN / EVIDENCE / COST lines. Read those four first; verify from EVIDENCE; accept with a verdict per critical item (non-critical items may be left open and said so).

## Delivery gate and waiting
Every request attached to a background task must be read (delegate result returning text) or cancelled before delivery. Until then send_document and send_email are blocked and the worker's final answer is not delivered.
- Result returned but unread: the final answer is bounced back to the worker, at most 3 times, then the task goes to needs_attention.
- Still running: the worker does not poll in a loop. Finish independent work, save a task_checkpoint and end the turn with a short interim note. The runtime parks the task as waiting_on_delegate, frees the worker slot for queued tasks, polls status every 90 seconds without model calls, and resumes the worker with a scope update when the result returns. After 45 minutes without a return the task goes to needs_attention and Alex is told (recalibrated 2026-10-06: real Cal/Vit jobs take 4 minutes at the median and 26 at most).
- Genuinely unusable result: the only bypass is a line in the final message: NOT DONE: Alex decided <X>; not carried out because <Y>. It is logged in the task's delivery_gate.json. Never use it to skip a result that is merely slow.

## Budgets: a target to size by, never a stop
Alex's position (2026-10-08, final): no budget ceiling of any kind. The agent doing the work decides whether burning more tokens is worth it and says so in the result. What must never happen is an agent working in a loop without knowing when to stop; that is caught by the runtime's loop detectors (an identical call is refused the third time; five failed or refused calls in a row end the worker with a diagnosis), not by a token number. Between 6 and 8 October the cap-and-pause regime stopped eleven productive tasks and caught no loop, so it was removed.

- Unit: effective tokens (uncached input ×1, cache write ×1.25, cached re-read ×0.1, output ×5). A turn costs about the context size, typically 25-45k.
- `budget_tokens` on a brief is the target the delegate sizes its work by and reports against. Size it from cato/reports/task_cost_history_*.md by type (median for routine, p90 for hard or novel). The runtime never pauses or refuses a delegate for passing it; the delegate's budget line says "over_target" and asks it to decide whether finishing is worth it.
- Every turn the worker sees its spend, its own estimate and the brief target. Use the number to plan, not to stop.
- Reporting: a result that passed its target says by how much and why it was worth it; a result that stopped early says what it judged not worth the cost. Never "ran out of budget".
- Fixed context: workers archive older bulky tool results once a request passes ~160 KB (full text stays retrievable with read_file). Keep briefs and context lean; every token of context is re-read on every turn.

## Finishing what was asked (2026-10-06, after Cal #17 returned with half the pack unverified)
What makes a delegate finish is a clear definition of done, a judgment slice first, and acceptance that checks evidence. The runtime keeps these; it no longer enforces any cap.

1. **Completion items, critical first.** Every submit carries numbered `completion_items` ({item, check, critical}), critical ones first, each with how Cato will verify it. The delegate sees the open critical items every turn. Write items you can check with a tool, a file, a number or a source; "thorough research" is not an item.
2. **Sizing from data.** `budget_tokens` is a target sized from the latest task_cost_history CSV; the sizing card shows the delegate's median for the type. A p90 scope with a p50 target is how #17 misjudged its work; the number informs the delegate, it does not stop it.
3. **Own estimate early.** The delegate sizes the whole job (task_checkpoint `estimate_effective_tokens`) early so its budget line can show drift against its own estimate. Nothing refuses a tool for lacking it.
4. **Acceptance, not reading.** `result` fetches the text; it does not count as read. Cato verifies each item (verify_artifact, review_output, recompute the number, open the source; consult_advisor for judgment calls, never a weaker model than the one that produced the work) and records `accept` with one verdict per item and the evidence. Critical items not met: `rework` (items, note) resumes the delegate on those alone. Delivery stays blocked until acceptance. "Looks fine" is not evidence.
5. **Raising the target.** `extend` sets a new target when Cato judges the job worth more; it is never needed to unblock a delegate, because nothing blocks one.
6. **Scorecard.** Each request ends with asked / delivered / verified how / cost against target / reworks, recorded automatically and reported to Alex with the deliverable.

## Step zero: decide whether to delegate at all
Alex (2026-10-06): multi-agent is a judgment call, never a reflex. Alex (2026-10-08): it is your judgment, with no form to fill in and nothing that refuses it. These are the usual reasons another executor gives the better output; name the one that applies in the brief's purpose:

- the work would bloat your own context: evidence gathering across many sources, long documents, large tables. Your solo tasks on 7 October re-read 144k tokens a turn; a delegate returns a compact result instead;
- units can run in parallel with your own work (Cal runs two jobs at once; a third queues behind them; Vit runs one);
- the delegate has a capability, source or skill you lack;
- you need a check you cannot credibly do on your own output.

I execute when none applies or when the handover would lose more than it gains, as with most short jobs. "Thorough", "safer", "Alex might like it" are not reasons; neither is "it would be cheaper alone" when the context cost is the real problem.

## Writing the brief: decompose, size, tier, know your delegate, then write
Cal #17 (2026-10-06) was an articulate brief that failed: five jobs in one, no arithmetic, no priority, a path Cal could not write to, a contradictory instruction ("ballpark" next to "unknown is unknown"). Clarity of language was not the problem. The method below is. The runtime refuses only a save path the delegate cannot write; the rest is your craft.

Before writing: read the delegate's card (`references/delegates/<agent>.json`: writable paths, tools, sources that work and fail, per-unit costs, brief habits) and two briefs from `references/briefs/` (accepted exemplars, counter-examples, `lessons.md`). Models imitate examples better than they follow rules; use that on yourself.

1. **Decompose.** One deliverable per request. At most 3 critical and 6 items; more is two requests, the second written after the first is accepted, with what you learned. An item that spans more than five units (names, files, papers, pages) is a request of its own, not an item: one oversized item beside others starves them, and the delegate runs out of room before the rest are met.
2. **Size.** Count the units and multiply by a per-unit cost from the card or task_cost_history; that is the target you put in `budget_tokens`. If the number looks too big, tier the units or split the request; never shave the target to make it look small.
3. **Tier.** Item 1 is the early judgment slice (`early: true`): the smallest piece whose result tells you whether the job is on track (the 8 tier-one names, the one module, the first table). The delegate finishes and checkpoints it first; you can read that checkpoint through status while it runs and rescope early.
4. **Know your delegate.** `output_path` inside its writable paths. Say what to do when a fact is not found: leave blank or write "not disclosed"; never "ballpark", never "estimate". One search pass per unit unless you say otherwise. Do not ask for what its sources cannot give (segment shares for small caps, turnover from text search).
5. **Write the check into each item.** "Exposure fact with IR URL per name; I open two URLs and recompute one share" is a check. "I read the file" is not. Your acceptance verdicts will have to cite exactly this.
6. **Stranger test, when you want it.** For a large or novel brief, have consult_advisor read it with no context before it goes and fix what it objects to. Your choice, not a gate.

Then the brief itself, shortened as appropriate. First line after the question: Decisions, copied from your checkpoint ledger (Alex: ... / Cato: ...), and what is out of scope; a delegate must never work from an assumption it cannot see. Then: question/output; decision relevance (which selection, ranking, risk or implementation choice it could change); inputs (evidence, dates, assumptions, paths); boundaries (universe, exclusions, allowed edits; required domain skill); limits (budget, passes, stop condition); return (finding, decisive sources, contrary evidence, unresolved gaps, saved artifact paths; flag blockers early). Pass Alex's decisions as decisions and your assumptions as assumptions, labelled.

At acceptance, a critical item not met costs you one sentence (`brief_lesson`): what the brief should have said or sized differently. It goes to `references/briefs/lessons.md`. First-pass successes are saved to `references/briefs/accepted/` automatically. Your briefs get better from your own record, not from being told to be clearer.

Delegates must not launch further delegates or broaden scope without the owner's explicit instruction. Owner retains responsibility. Engineering success does not prove financial correctness; source matches do not prove truth.

## Clean verifier for deliverables
Before a basket, screen, evidence pack or workbook goes to Alex, have it verified by a fresh Cal job that gets only three things: the artifact path, the acceptance criteria (the skill's Output section and the completion items), and the tools. Not the history, not your draft reasoning, not the sources you leaned on. Its completion items are the criteria, one verdict each with the evidence it opened. This is the single highest-value step in the failure research (MAST: an objective-level verification step was worth +15.6 points, the largest measured gain; Anthropic names the "early victory" verifier that runs a few checks and declares success). review_output and verify_artifact remain your own first pass; the clean verifier is the second, and it is what you quote to Alex as "verified how".

Every delegate result ends with a `[Route]` line naming the model route that did the work (Cal runs ChatGPT first, you run Claude first, so his check is normally cross-model). When his route shows the same family as yours (the ChatGPT route was cooling down), the pass still counts, but say in the scorecard that it was not cross-model. Size it lean: the verifier's completion items are the two or three critical criteria (decisive sourced claims, identifiers/dates, structure), one fetch per claim; leave open-ended omission or discovery searches out unless they could change membership, and budget from Cal's observed per-turn cost on the active route (2026-10-08 task 103 and 2026-10-09 task 116 both ran about 6-7x target with five-criterion verifier briefs). Never skip the verifier to save tokens: the estimate is a target, not a cap (Alex, 2026-10-08; task 106 skipped it "to contain cost" and that was wrong).

## Focused challenge
For a conclusion that becomes a market view or goes to a client, Cal's challenge pass (his tenth-person skill) is on by default: I do not decide whether to have it. For other work I decide, and Cal does not add a reviewer on his own. Either way it is ONE focused challenge pass, never a second research project or a stack of reviewers. Cal runs on a different model from mine on purpose, so the pass is worth having. Brief it blind first: the original question and the primary data or source paths, then the draft path last with the instruction to write his expected conclusion and likely failure points before opening it. Give the decisive evidence, including contrary evidence. Ask for the three weakest material claims, each with the source checked, the contrary evidence and what would overturn it; an "agree" without a checked source is not a pass. When I do not adopt one of his objections, Alex sees Cal's words and my reason side by side and decides; I never close a disagreement silently.

An additional pass requires a written exception: named unresolved issue, potential consequence, exact bounded check and cost justification within the remaining allowance. Agreement is not verification. Resolve disagreements against original evidence and actual calculations. Stop if no material uncertainty remains.

## Decision-change gate
Before additional research, tool-heavy expansion, another delegate or review round, answer: What decision could the next step change?

Proceed only if a plausible result could materially change the deliverable or its safety and the bounded check is worth its cost. New detail, polish or reassurance alone does not qualify. When benefit is exhausted, stop and deliver with explicit limitations. Optional data gaps must not hold up a supported deliverable. If a missing fact threatens the central conclusion, narrow or withhold that conclusion; do not fabricate certainty. Mandatory safety and task-specific artifact QA remain required, kept targeted.

## Acceptance and recovery
Every delegate states its assumptions in its first checkpoint (guesses) or asks a QUESTION; the status and result replies show them. At accept you give guesses_verdict: 'ok' when each matches what Alex meant, or 'wrong: <which, and what was meant>', which counts as a rework and sends the affected items back. A wrong guess is a brief defect: say in brief_lesson what the brief should have said. Inspect returned evidence before claiming completion. For integration bugs, require reproduction and correction in the consuming application or workflow; component checks alone leave the incident open. Report verified improvements separately when reproduction or consumer testing is missing. Check worker status for progress; queued is not running. Unknown external outcomes must be inspected before any replay. Follow existing cancellation/control mechanisms; a parent cancellation does not prove delegates stopped. On a stop or cost objection, halt new work and control active delegates where authorized; state any unconfirmed stop honestly. Preserve checkpoints and reusable evidence. Resume only within the user's direction.

## Minimal contribution record
Use existing task/project audit records, not a new logging system. At completion record each delegate's purpose, decision-changing correction or bottleneck resolved, reported token usage and elapsed time when available, and unresolved limitations. Unknown usage stays unknown. Separate qualitative benefit from measured cost; do not invent a benefit score or quota percentage. Successful use proves only the checks actually performed.

## Trigger examples
- Build a basket: delegate distinct personal discovery, then one focused challenge of actual selections; stop discovery when new findings no longer change membership or exposure tiers.
- Build an interface: give Vit a scoped implementation contract; accept only after artifact checks, not a completion message.
- Routine factual question: answer directly when evidence is sufficient; no reviewer for ceremony.

## Enforcement boundary
Enforced by the runtime: the loop detectors (identical call refused the third time; five failed or refused calls end the worker), the no-poll refusals, the delivery gate (unread delegations block delivery), and the skill output contract check before a background result is delivered. Budget numbers are never enforced (Alex, 2026-10-08). Everything else here is operating instruction, not a wrapper limit. Do not claim universal triggering or validated savings without a tested implementation. Keep connection details in memory and implementation in existing wrappers. Installation alone does not refresh a runtime's startup-only skill catalogue; verify discovery and disclose reload requirements.

