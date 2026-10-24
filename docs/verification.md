# Requirement verification

The old acceptance gate answered “did a tool run recently?” It did not establish that the
specified requirement had been checked. It accepted both unrelated successful actions and failures.
the owner authorized replacing that gate on 2026-10-23.

## One owner of proof

`agent_core/verification.py` owns check execution and the interpretation of its records.
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

Acceptance derives its verdict, evidence path and ran/read provenance from the check. Model prose is
stored separately as assessment. Accepting review of a report is allowed but counts as unverified,
not met. `accepted` means reviewed; `verified` means every requirement passed. Omitted optional items
also remain unverified. No unverified result becomes a successful exemplar.

Own and delegated task delivery displays current passed/failed/unverified status. Old tasks without
requirements and old activity-only acceptance records are not grandfathered into proof. Their task
history is preserved. Changed artifact bytes, dependencies, requirements or a rework invalidate proof.

## Limits and review responsibility

The harness proves that a declared test executed against those bytes and reports its outcome.
It cannot prove that an arbitrarily written test is a good test, or turn subjective quality into a
fact. Review the assertions against the requirement before declaring the check. A receipt is a local
operational record, not a security boundary against an agent deliberately editing its own disk.

Rejected alternative: add more filters to generic action receipts. Even a successful action on a
related path does not establish that the requested behavior was exercised. That path was deleted.

## Tests and release

`tests/test_acceptance_run.py` exercises acceptance with real files and a temporary TaskStore.
`tests/test_requirement_verification.py` exercises real command exits, all four agents' tool paths,
scripted model turns, stale dependencies, interrupted checks, role restrictions and concurrency.
Original unrelated-success and failed-action regressions failed before the change and pass after it.
Run both repository unittest suites before release. No memory or conversation migrations are needed.
Restart through agent_control's normal drain process, after telling the owner.
