# Durable task requirements

Authorized by the owner on 2026-10-23. Backup: Git parent 83edd0e. No memory edits.

## Failure and replacement

The carrier-basket incident established the harm from losing part of a request. Its original
instruction-delivery bug was fixed earlier. This change addresses a separately reproduced remaining
failure: checkpoint replacement erased an unfinished item; redeclaring the old checklist after a
new instruction refreshed its scope hash and let the task finish; delegation rescope shrank its list.
All three regression tests failed on the prior release, including finished instead of needs_attention.

The shared verification owner now stores task requirements in SQLite (requirements, in jobs.sqlite3).
Initial requirements supplied to background_task start are saved atomically before worker launch.
Original brief and later instructions remain the existing tasks/controls records, not new copies.
Every ordinary task must cover source 0 (brief) and each delivered instruction ID with obligations.
A source reference proves provenance/coverage bookkeeping, not semantic interpretation correctness.

A requirement's position, item text and source are stable. Checkpoint completion_items merges test
updates and new obligations; omission never deletes an existing item. An id identifies an existing
requirement when changing its check; the next unused id appends a new obligation on either provider. New instructions cannot be cleared by redeclaring/rechecking
the old list. Pending controls also continue to block completion through the existing decision.
The full current requirement set is pinned during work, and current proof is bound to the scope
and complete contract. Existing requirement verification and task_decisions.finish remain the only
proof/completion owners; no judge, new model pass, retry loop or approval layer was added.

Delegation submit/rescope and the Cal/Vit clients preserve the complete ordered contract. Remote
source IDs are local to that delegate; parent_item/parent_source preserve caller provenance. The
scorecard and budget/status views derive current items from the same durable store. Rescope can add
obligations or revise tests; it cannot silently remove existing obligations.

Removed: checkpoint ownership of completion_items and requirement_scope, wholesale scorecard
item replacement, remote rescope replacement, and scope-refresh-as-coverage logic. Old checkpoint,
context and scorecard lists are read once as migration input when a task is accessed; they are not
live authorities after migration. No bulk scan of private conversations or memory is performed.

## Validation and limits

Final suites: 377 shared tests and 92 Cato tests passed, and both passed again through the restart command. Tests cover all four real agent objects:
missing and omitted obligations, new instructions, restart persistence, original and later items
verified successfully, invalid/reassigned sources, stable numbering, intake before launch, legacy
migration and stale checkpoint overwrite attempts, parent references, and derived reporting.
The real subprocess decision test still asks, resumes once, verifies both original work and the
later answer, and finishes. Verification tests now update a stable obligation's check explicitly
rather than replacing the list with a differently named requirement.

The initial natural-language decomposition is still model judgment. This change does not prove
that a weak test or a poorly written initial checklist represents every nuance of the user's words.
Subjective requirements stay unverified. Scope reduction is not inferred from omission: old items
remain outstanding; no worker-facing waiver or automatic deletion mechanism was introduced.

The live comparison uses the same small invoice-v1 oracle, k=1, same refund steer and model routes
as before. It measures delivered quality and whole-task tokens. The fixed benchmark oracle is
independent of worker-authored checklists; ordinary-task omission/continuation behavior is tested
separately above. This is not a claim of general research quality or deterministic token savings.
Post-restart measurements will be appended after running the matched cases.

## Released results and failures retained

Final runtime release: a81219d (e58006f durable storage; 80029ed fixed benchmark ownership;
a81219d explicit new IDs). Restarts were announced before each request and ran both full suites.
All four final benchmark workers recorded a81219d. Quality was 16/16 requirements at every stage.

| Agent | Input before | Final input | Output before | Final output | Responses before -> final |
|---|---:|---:|---:|---:|---:|
| Cato | 46,128 | 46,984 | 658 | 634 | 3 -> 3 |
| Cal | 22,392 | 15,280 | 286 | 216 | 3 -> 2 |
| Vit | 41,151 | 58,618 | 746 | 1,286 | 4 -> 5 |
| Tacitus | 63,250 | 64,596 | 991 | 982 | 4 -> 4 |
| Total | 172,921 | 185,478 | 2,681 | 3,118 | 14 -> 14 |

Final input was 7.3% higher, output 16.3% higher. No token saving is claimed. Cache-read tokens
fell from 137,560 to 117,107; the weighted local usage proxy rose from 67,200 to 108,940.
This proxy is not subscription quota. Small k=1 runs, changed cache state and differing model actions
prevent attributing the whole delta to the code. Quality was unchanged on the small oracle; the
reliability gain is separately demonstrated by previously failing omission tests.

The FIRST after-run (retained in requirements-first-after.json) used 381,139 input tokens and 9,167
output tokens, with 26 model responses. Fixed benchmark tasks had their own oracle yet were told to
build a second checklist. Tool receipts confirmed redundant checkpoint/verification calls. The
working context now reads the same validated preset contract as completion and reveals criterion
descriptions only, never expected answers. No ordinary-task requirement gate was weakened.

Ordinary completion was also tested live, without using the preset benchmark completion branch:
Cato task 327 passed both original towers and later carriers. Cal task 66 initially wrote both files
but could not register carriers: its provider supplied a required explicit id, while new-item handling
expected omission. It correctly remained needs_attention. The shared protocol now accepts the next
stable number, and the same Cal task resumed from its checkpoint after restart, verified both files,
and finished on a81219d. Initial failure and usage: requirements-live-initial.json. Final cumulative
Cal usage was 351,936 input / 5,255 output (24 responses, including the 17-response failed attempt);
Cato's smoke used 129,108 input / 1,558 output (5 responses). These are test overhead, not savings.
Total recorded provider usage across the three benchmark stages and two live flows (Cal counted
cumulatively once): 1,220,582 input / 21,779 output tokens. Offline tests make no paid model calls.

A separate operational issue delayed the last restart: three clean exits were treated as a crash
loop. Queued tests survived and all agents recovered automatically. See the incident record
incidents/2026-10-23-clean-restarts-delayed-agents.md. Supervisor policy was not changed in this work.
