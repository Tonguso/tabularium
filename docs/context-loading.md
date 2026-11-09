# Context loading redesign, 2026-10-23

The old request builder treated the agent's whole working state as relevant to every worker.
It also placed every optional skill description in every system prompt. A worker inherited
unrelated tasks/questions and current projects; its own full contract was pinned only after
history trimming, with brief/update truncation. These are ownership problems, not a need for
another summarizer.

Design considered: model-selected summaries of all context, or explicit ownership plus retrieval.
The latter preserves authoritative text and removes the global-state path from workers. No new
summarization service, vector database, routing heuristic, or additional model call was added.

## Measurement before changing code

Previous live benchmark: docs/evaluation-live-baseline.json, two cases per agent, k=1.
Generation input/cache-read/output tokens were Cato 162156/122205/1335; Cal 55238/0/513;
Vit 87001/69420/1296; Tacitus 86908/56221/2120. Input includes cache reads. These are accumulated
provider-reported tokens across responses, not the size of a single prompt or quota units.

Saved Claude worker system prompts: Cato 53807 bytes, Vit 32387, Tacitus 30338. Cal uses Responses
and had no corresponding saved system-prompt file. Optional/standing skill catalogue metadata
(name plus description) totals: Cato 14732 characters/30 skills; Cal 8424/19; Vit 10421/27;
Tacitus 2402/4 (its standing standing method remains in full).
At inspection time, current memory projection/live task projection sizes in characters were:
Cato 12741/2765; Cal 2227/0; Vit 2124/0. These are current measurements, not reconstructed
historical payloads. Tacitus memory and conversation were not inspected or measured offline.

## Replacement

- Workers receive live task evidence and pending decisions scoped to their own task. Foreground
  requests retain the cross-task overview and all pending questions.
- Core memory and sourced preferences remain current. Worker requests omit unrelated working
  threads, recent activity and project-register summaries. Those remain retrievable via read_memory.
  No memory file was changed; no arbitrary filtering of personal instructions was attempted.
- The initial skill index carries names. Existing skill(name="") returns full descriptions;
  skill(name="exact-name") returns the method. Standing skills, identity, policy, tool definitions,
  and the communication core remain loaded. Skill selection still requires model judgment;
  this change does not claim that all specialized tasks have been evaluated.
- Every worker request starts with its complete durable brief, acknowledged scope updates
  (including verified decision answers), and current completion requirements/next action/decision
  ledger. Missing or invalid contract data fails loudly. Saved conversation is not modified.
  Mid-session updates retain the existing delivery mechanism, including the SDK tool-result path.
- Removed the brief/update truncation constants, catch-and-continue missing-contract path,
  unconditional description catalogue and global working-state projection from worker requests.
- context_sizes.json under each runtime's task_state records integer character counts only:
  system, Anthropic-shaped tool schemas, prepared messages and the context components within
  messages. Components overlap with the message total; do not add them twice. This is the host
  request/session-start projection, not the SDK's hidden internal requests or exact provider tokens.

## Verification and comparison limits

New regression tests first failed on the old code: unrelated decisions/working state were loaded,
long requirements were lost, optional descriptions were always injected, and no size diagnostics
existed. Tests use real agent objects for all four roles, actual SQLite decision resolution and
reopened stores, real checkpoints, short/trimmed histories, and protected compaction. Foreground
questions, fresh core preferences, and standing methods remain available. Skill catalogue lookup
is tested, but the two live benchmark cases do not measure specialist skill-selection quality.

Concurrent release dd004a0 changes how isolated evaluations handle questions and correction bursts.
It also captured the context(store, chat, task) helper edit while this redesign was in progress.
Those changes are preserved. Any Tacitus quality change cannot be attributed solely to context
loading. The live comparison is a small observational baseline, not a controlled causal estimate.

Full-suite and post-restart live results are recorded below.

## Release and live results

Code release e8a536d; both full suites passed (362 shared, 92 Cato). Compilation and whitespace
checks passed. All four agents completed the announced graceful restart; new PIDs were confirmed.
All eight live attempts then completed under e8a536d, using the same benchmark fingerprint and k=1.

| Agent | Requirements passed | Input tokens | Cache-read portion | Output tokens | Responses |
|---|---:|---:|---:|---:|---:|
| coordinator | 100% → 100% | 162,156 → 93,235 (-42.5%) | 122,205 → 66,474 | 1,335 → 1,264 | 7 → 6 |
| librarian | 100% → 100% | 55,238 → 36,363 (-34.2%) | 0 → 0 | 513 → 453 | 6 → 5 |
| engineer | 100% → 100% | 87,001 → 54,341 (-37.5%) | 69,420 → 40,791 | 1,296 → 1,376 | 7 → 6 |
| private | 50% → 100% | 86,908 → 104,307 (+20.0%) | 56,221 → 78,417 | 2,120 → 1,374 | 6 → 7 |

All 32 requirements passed after the change (28/32 before). Grading still costs zero model calls.
Combined input tokens: 391,303 → 288,246 (-26.3%). This is an observed result,
not a promise of the same saving on ordinary tasks or provider subscription quota. Response counts
changed. Tacitus completed an additional case and used MORE total input tokens; its earlier question
stop and concurrent evaluation-policy change prevent attributing that quality difference to loading.
Provider/model/billing routes match the baseline: Cal OpenAI gpt-6.1-sol subscription; others
Claude SDK claude-opus-5-5 subscription. No benchmark failure was retried away.

Comparable saved system-prompt character counts (excluding the SDK active-model suffix):
Cato 52,892 → 38,269; Vit 31,912 → 21,584; Tacitus 29,759 → 28,030.
Cal's new system prompt is 22,535 characters; a saved pre-change prompt was unavailable.
Invoice-task context counts after the change:

| Agent | Core characters | Own live task characters | Contract characters | Prepared messages characters |
|---|---:|---:|---:|---:|
| coordinator | 7,569 | 374 | 512 | 13,258 |
| librarian | 1,270 | 373 | 512 | 6,581 |
| engineer | 1,193 | 373 | 512 | 4,753 |
| private | 14,856 | 372 | 512 | 17,868 |

The tool schemas are 5,316 characters in these restricted benchmark workers. Ordinary workers have
larger tool sets. The core, live state and contract are already included in prepared message counts.
Tacitus's numbers came only from the runtime's integer diagnostics, not inspection of private text.
Full synthetic results, per-attempt routes, deltas and numeric context diagnostics are saved in
context-loading-comparison.json. No additional restart is required for this report.
