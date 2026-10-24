# Requirement workflow efficiency, 2026-10-24

the owner authorized removing duplicate work across all agents, preserving unrelated proof, testing ordinary
continuation with later requirements, and comparing quality and tokens. Baseline: 8356034.

The contract remains the single durable requirement store. Proof now binds one requirement and its
source instruction plus exact artifact/test dependency bytes, not the entire growing task list or
rework counter. Adding an obligation cannot clear another obligation or mark the new one done.
Completion still rejects uncovered instructions, missing/failed/stale proof, pending controls and
unresolved decisions. Changing a test during execution yields unverified. Currently valid legacy
receipts upgrade under the existing proof lease; already stale legacy receipts remain unverified.

Working context renders requirements with derived proof status. Checkpoints acknowledge the save
without echoing the saved ledger or requirement list; lesson feedback is retained. Budget notices no
longer carry another checklist based on items_done. Progress saves no longer require an invented
assumption, a plan for every small task, or a final bookkeeping call. Material assumptions still need
review at delegation acceptance; blockers and decisions remain durable. Fixed benchmark tasks expose
only their runtime-owned verification workflow: worker checklist declarations and manual verification
are unavailable in both tool schemas and dispatch. No added judge, retry loop or model pass.

Deleted: role-based first-checkpoint rejection and constant, duplicate checkpoint requirement/ledger
output, budget open_items and focus generation, full-contract proof invalidation, and mandatory
completion/plan bookkeeping instructions. Shared behavior applies to all four roles and both providers.

Tests cover omission, later instructions, unrelated proof retention, individual test/file/dependency
invalidation, failed/interrupted checks, identity-bound proof, old-receipt migration, concurrent check
changes, real model-turn dispatch for all roles, and preset tool restrictions. Existing tests that
expected blanket invalidation or duplicate reporting now assert the replacement behavior.

Live measurement: `python tests/ordinary_workflow_probe.py LABEL` explicitly spends subscription
usage. Each agent writes and verifies towers.txt, completes, reopens on a later instruction, writes
and verifies carriers.txt, and completes again. This uses ordinary completion in isolated synthetic
workers, with no fixed benchmark binding. Files are checked independently for exact contents; the
runtime separately requires recorded proof. Only synthetic actions and numeric usage are inspected.
Raw task records remain under each task folder; compact results are in requirements-efficiency-comparison.json.
One matched run per agent is observational: cache differences and model choices preclude a guaranteed
saving. Requirement interpretation and test strength still require judgment; no semantic completeness
claim is made. Offline regression suites do not make paid model calls.

Before: all 8 files correct, all 4 continuations finished; 873418 input, 9266 output, 45 responses.
Offline validation: 385 shared + 92 Cato tests passed. After-release results pending; restart announced.


The first live after-run passed all files, but agents still requested repeat verification. Inspection
found the resume path also copied the saved checkpoint into history with an instruction to verify
evidence before acting. Removed that producer: current checkpoint facts, progress, assumptions,
evidence, blockers and decisions now ride in the current contract, never as a new saved history copy.
Verification now ensures current proof: an unchanged pass returns the existing receipt without tool
execution or overwriting its timestamp. Explicit rerun=true retains fresh-execution semantics, including
invalidating a prior pass on failure/interruption. Changes to tests/files/dependencies always execute.
Tests reproduce and cover both resume duplication and redundant execution through all four agents.

Concurrent Cato skill commits 2d74464/b7e9b52 occurred during the first after-run. They are preserved,
not part of this runtime change. Cato's usage is consequently confounded by that catalogue change.
First after results are retained separately rather than silently replaced by the final run.


## Final release and comparison

Runtime release 7b67d49. Both successful restarts were announced and passed the full test gate;
final gate: 388 shared + 92 Cato tests. All four final workers recorded this runtime release.
The first restart attempt was refused by the test gate, before any drain, due to the documented
crash-test teardown race. The test-only ownership/shutdown fix was verified 20 times.

| Agent | Input before | Input final | Output before | Output final | Responses before / final |
|---|---:|---:|---:|---:|---:|
| cato | 301595 | 261339 | 2454 | 2224 | 11 / 10 |
| tacitus | 151987 | 148339 | 2153 | 1906 | 8 / 8 |
| callimachus | 183587 | 133678 | 1737 | 1436 | 14 / 11 |
| vitruvius | 236249 | 187983 | 2922 | 2114 | 12 / 10 |

Total input 873418 -> 731339 (-16.27%); output 9266 -> 7680 (-17.12%); responses 45 -> 39.
All eight output files passed independent exact-byte checks (normal terminal newline allowed), and
all four ordinary completion records passed with no uncovered instructions. Three repeated proof
requests reused the old receipt without running another check. Models can still request a redundant
lookup; the runtime does not manufacture a new verification execution from that request.

Checkpoint response text fell from 10405 to 308 characters across these runs. Cache-read tokens
were 589501 -> 513995 (cache share 67.5% -> 70.3%). The local weighted proxy fell 20.0%; it is not
subscription quota. The final run used slightly more raw input than the retained first after-run
(731339 vs 717697); model actions vary, and this is not evidence every individual change saves tokens.

Cato's final queued probe 333 timed out without ever launching (zero usage) behind a real task holding
its worker lease while waiting for a decision. Only probe 333 was cancelled. Replacement 334 used the
existing Eval lane prefix, but still ordinary verification/completion with no preset benchmark binding.
The different prefix and concurrent Cato skill/profile edits confound exact causal attribution for Cato.
Other final tasks: Cal 70, Vit 37, Tacitus 12. Real waiting work was not interrupted or modified.

The three live measurement stages consumed 2322454 input tokens (including cached input), 24214
output tokens and 123 responses in total. These are one-time test costs; offline suites use no provider
calls. No further live repetitions were run. The queue timeout is retained in the machine-readable report.

Incident review: incidents/2026-10-24-redundant-requirement-work.md. No memory files changed.
No further restart is needed for this results-only documentation and test-driver CLI update.
