# Requirement verification

The old acceptance gate answered “did a tool run recently?” It did not establish that the
specified requirement had been checked. It accepted both unrelated successful actions and failures.
the owner authorized replacing that gate on 2026-10-23.

## One owner of proof

`tabularium/records/verification.py` owns check execution and the interpretation of its records.
All four agents use `verify_requirement`. `TaskStore.accept_delegation` enforces the same rule,
including callers that bypass Cato's tool handler. No extra reviewer model or supervisor is involved.

A completion item has its existing item/check/critical fields and an optional verification:

```json
{
  "item": "The delivered executable processes a dropped video",
  "check": "Launch this build, drop a fixture video, wait for a playable output, assert the result",
  "critical": true,
  "verification": {
    "kind": "command",
    "paths": ["D:\\work\\Tiro.exe", "D:\\work\\test_drop.py", "D:\\work\\fixture.mp4"],
    "argv": ["C:\\path\\to\\python.exe", "D:\\work\\test_drop.py", "{artifact}"]
  }
}
```

This is an example contract, not an existing Tiro test or evidence that Tiro works.
The test script must perform the actual UI interaction and assertions; merely launching a process
is insufficient. Include the delivered files and relevant test dependencies in paths. Only files
are accepted; for a multi-file application enumerate the files that affect the tested behavior.
The runtime substitutes the primary artifact as a safely quoted, entire argument. One test program
runs through the existing command tool and permissions, with its exit code preserved. A nonzero
exit is a failure. A refusal, missing file, interrupted run, or changed file is unverified.

For a literal textual requirement, use kind=contains, paths=[absolute file], text=required literal.
This proves text presence only, not factual correctness, application behavior, or visual quality.
Tacitus can use this check without gaining command access. Other kinds of review remain judgments.

Declare the items in delegate submit/rescope for owner-side acceptance, or task_checkpoint for own
work. Then call verify_requirement(task_id=..., item=..., request_id=...) for each delegation item;
omit request_id for own work. The returned record contains the observed tool output, inputs, hashes,
and scope. The latest check owns the current status; overlapping runs are refused using WorkerLease.
A rerun replaces prior proof, including when the rerun fails or is interrupted.

## Delegation agreement and acceptance (2026-10-25)

Mapped delegations use the delegate task's requirements and controls as the authoritative agreement.
Cato resolves the existing request mapping instead of maintaining a second changing checklist.
Rescope updates that agreement once, preserving item IDs and instruction sources. Answers and later
worker-declared requirements are visible in the agreement Cato fetches. Original briefs and legacy
owner rows remain historical/migration evidence. Unmapped legacy requests cannot acquire acceptance.

`delegate(result)` returns text, the current agreement/instructions and `delivery_id`.
`delegate(accept, delivery_id=..., verdicts=...)` reviews that delivery. Its identity includes worker
task/attempt/update, agreement/instructions, result bytes, declared check files and explicit output file.
Running work, unresolved instructions/questions, missing results, changed delivery, stale ID or a
missing item review cannot become accepted. The binding uses the existing delegations.acceptance JSON.

Review and proof are separate. Executable checks require matching current proof. Subjective items
accept read/judgment assessments with reasons, without claiming executable proof. Report alone cannot
establish success. Explicit rejection stays rejected even if a machine check passed. Every item,
including noncritical items, needs review; partial/rejected items stay open for targeted rework.
`accepted` means every item was accepted for this delivery; `verified` additionally means all items
have executable proof. Ordinary single-agent subjective completion remains unchanged; no message
prefix is added.

`verification.acceptance` is the single decision used for recording review and owner completion.
Fetching text does not grade it. New work invalidates delivery acceptance, not unchanged proof.
Worker and reviewer share requirement-bound receipts. Test/requirement/file changes invalidate the
affected proof; other proof survives scope additions and rework. Compatible legacy owner proof migrates
only after its original binding and current file hashes match, and never overrides a worker receipt.
Old unbound acceptance needs a fresh fetch/review. Historical completed tasks are not reopened.

Removed: Cato's separate verdict validator, duplicate normalization, owner-side rescope write,
initial-brief validation on rescope, and completion's separate subjective-blind dependency check.
The acceptance response no longer echoes the full scorecard and original brief.

## Controlled comparison

Five reproduced failure scenarios: 0/5 correct before, 5/5 after. This defect-focused set is not an
overall agent-quality score. The ordinary objective workflow remains accepted and verified in both
versions. Check executions fall from two to one by reusing the delegate's matching proof.
Tool-result text measures 639 versus 503 tokens with o200k_base (about 21% smaller). This synthetic
replay excludes prompts, tool-call arguments and generated model text, and is not subscription usage.
Both versions make zero model calls. No live-model quality or quota improvement is claimed.
Inputs/results are retained in backups/delegation-agreement-20261025; compare.py must run from the
repository import root, not from the backup directory, which contains historical modules.

## Limits and review responsibility

The harness proves that a declared test executed against those bytes and reports its outcome.
It cannot prove that an arbitrarily written test is a good test, or turn subjective quality into a
fact. Review the assertions against the requirement before declaring the check. A receipt is a local
operational record, not a security boundary against an agent deliberately editing its own disk.

Rejected alternative: add more filters to generic action receipts. Even a successful action on a
related path does not establish that the requested behavior was exercised. That path was deleted.

## Tests and release

`tests/test_delegation_agreement.py` covers both real clients, answers, rescope, rework, stale reviews,
subjective rejection, full completion and legacy proof migration in isolated stores.
`tests/test_acceptance_run.py` retains unrelated/failed/stale-proof coverage.
`tests/test_requirement_verification.py` exercises real command exits, all four agents' tool paths,
scripted model turns, stale dependencies, interrupted checks, role restrictions and concurrency.
Original unrelated-success and failed-action regressions failed before the change and pass after it.
Run both repository unittest suites before release. No memory or conversation migrations are needed.
Restart through agent_control's normal drain process, after telling the owner.
