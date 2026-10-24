# One completion decision, 2026-10-23

The worker previously ran a scope self-check, a skill-output self-check, up to three delegation
retries, reply handling, then a separate status decision. Scope/format errors returned the old draft.
The final status trusted task_outcome=completed and a clear blocker; it did not require current
artifact proof. A NOT DONE: line bypassed unaccepted dependencies. This combined repeated model
work with an acceptance gap.

## Measurement

Before changing code, one synthetic invoice task plus an added refund instruction ran on each
agent. All four artifacts passed 4/4 requirements. Total generation input/cache/output tokens:
Cato 70721/56377/1414 (5 responses); Cal 30487/0/513 (4); Vit 58217/48652/1856 (6);
Tacitus 85210/67176/1728 (6). Input includes cache reads. These are whole-task provider totals,
not separately instrumented gate costs. Raw records: completion-before.json.

A controlled unchanged-draft replay of the original functions requested one scope review, one
format review and three dependency retries. The model in that replay was a counting fixture;
no provider tokens are claimed for it. Raw measurement: completion-gate-replay.json.

## Replacement and deletions

TaskStore.finish delegates to task_decisions.finish, the single owner of completion state. It
checks durable pending questions/actions, undelivered controls, dependencies, and current proof.
A completion.json record explains the state and every failed/unverified declared requirement.
The worker presents an explicit runtime notice when its draft was not verified.

Ordinary work requires nonempty completion_items, a completed envelope, no blocker, all declared
requirements passed, and current proof for accepted dependencies. Every declared item counts,
not only critical ones. Requirements/proof are bound to the original brief and delivered scope
updates; new instructions require renewed declaration and verification. Old checkpoints without
that scope binding are unverified, not grandfathered in. Current hashes must still match.

In-flight dependencies park through the existing lifecycle and wake on return, with no model loop.
Returned-but-unaccepted work needs attention. User questions remain durable; a late answer or new
control cannot be overwritten by a worker claiming completion. Cancellation and worker ownership
checks remain authoritative.

Trusted eval tasks use their fingerprinted independent artifact oracle. Missing or incorrect
artifacts cannot finish successfully, even if the model declares completion. Diagnosis/lesson-trial
records can end as recorded, passed=false: ending a record is not verified successful work. Isolated
runs still cannot ask the owner; this preserves the intent of the prior author change while replacing
its blanket finished status for failed evals.

Removed apply_scope_gate, apply_format_gate, apply_delivery_gate, the NOT DONE: bypass and duplicate
worker verify_reply call. Loaded skill Output sections now accompany the pinned working contract,
rather than generating another full-history turn after the draft. Foreground status-claim logging
is unchanged. The completion decision itself makes zero model calls and introduces no judge service.

## Limits and validation

Subjective/report-only requirements remain unverified and need attention; this release does not
invent machine proof or add a subjective judge. Someone still must choose a complete, meaningful
requirement set and adequate tests: passing a weak or incomplete declared checklist cannot establish
semantic fulfillment of every sentence in an open-ended request. Skill Output context is preserved,
not automatically proven by its presence. Those are explicit limits, not claimed solved capabilities.

Tests failed before the fix for missing/failed/stale proof, missing criteria, corrupt checkpoint,
missing eval deliverables, prose bypasses, and a changed scope with old proof. Real four-role tests
exercise verification. A real subprocess asks, survives reload, resumes once, writes an artifact,
verifies it and finishes. A subprocess that merely claims success now needs attention. In-flight
dependencies park and wake without model calls. Obsolete gate-call expectations were removed or
replaced, not retained as tests of the new architecture.

Both complete suites pass: 366 shared and 92 Cato. Changed Python compiles; no new with statements
in replacement code. No memory files changed, and Tacitus private content was not inspected.
Code backup: Git parent 1b32291; original completion functions also saved in ignored scratch files.
Post-restart matched benchmark results follow separately.

## Release comparison

Released fd92eb3 through the normal graceful restart after notifying the owner. The restart command
reran both complete suites successfully. All four agents restarted; the four synthetic after-run
workers recorded this release and completion.json passed=true. No memory changes.

Matched invoice-v1, k=1, identical oracle and provider/model routes, with the same durable refund
scope update in both runs. All 16 requirements passed before and after; no grading model calls.

| Agent | Input before | Input after | Output before | Output after | Model responses |
|---|---:|---:|---:|---:|---:|
| Cato | 70,721 | 69,551 | 1,414 | 778 | 5 -> 4 |
| Cal | 30,487 | 22,392 | 513 | 264 | 4 -> 3 |
| Vit | 58,217 | 41,177 | 1,856 | 757 | 6 -> 4 |
| Tacitus | 85,210 | 63,188 | 1,728 | 895 | 6 -> 4 |
| Total | 244,635 | 196,308 | 5,511 | 2,694 | 21 -> 15 |

Input declined 19.8%; output declined 51.1%. Cache-read tokens were 172,205 -> 155,036.
The local weighted usage proxy was 127,685 -> 74,960; this is not a subscription quota measurement.
This is one small case per agent: model variation and cache state affect totals. It establishes no
quality regression on these checks, not general research-quality equivalence. The controlled replay
separately established removed gate calls; live totals cannot isolate each gate's exact token cost.
Raw results and matched comparisons: completion-comparison.json. After task IDs: Cato 323, Cal 63,
Vit 30, Tacitus 6. The unused TaskStore.park wrapper was also deleted; the completion decision now
owns production parking, while lifecycle tests exercise named transitions directly.
