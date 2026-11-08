# Evaluation redesign, 2026-10-23

The old evaluator compared a reply with an earlier agent reply, let the judge choose which plan
items to return, and treated any nonempty all-true subset as a pass. It never opened the deliverable.

The shared evaluator now owns the requirement set. Every ID receives passed, failed or unverified.
A missing, repeated, invented or malformed subjective verdict invalidates that judgment batch.
Objective requirements inspect the delivered JSON field, text file or workbook cell, recording the
path and SHA-256 of the bytes actually checked. Wrong data fails; unreadable/missing data is unverified.
A confident reply is not a substitute. Subjective rubric judgments remain explicitly labelled.

Default cases live in src/tabularium/learning/benchmark_cases.json, with version, independent expected values and
oracle provenance. The original two synthetic cases cover four invoice checks and four source/correction checks; four research cases extend them (see below).
They apply to all four roles. Codex checked their arithmetic/source facts; they are not claimed to be
the owner-approved answers or a comprehensive measure of research, coding or private conversation quality.

Historical evals/cases.json files are preserved as candidates only. --collect collects candidates and
exits; it never promotes agent prose to ground truth. Tacitus's private history cannot be collected.
Benchmark worker tools are limited to reading/writing local files, checkpoints and verification;
no shell, web, sends, memory edits or delegation. These restrictions apply to these closed-book
synthetic cases, not ordinary agents. Lesson trials use the stricter scope described below. The normal agent context may still affect a run;
this benchmark is not a test of memory isolation or of every production tool.

Run `tabularium eval <agent> --k 1` for a first baseline, then repeated runs (--k 3) when
measuring reliability. Names: cato, callimachus, vitruvius, tacitus. Results retain each requirement,
artifact evidence, release, routes, generation usage and grading usage. Unknown usage is labelled
unknown; recorded subtotals are not claimed to be complete. Effective-token weights are the existing
accounting heuristic, not provider quota units. Only the same cases/oracles/evaluator version/k are
compared; the dashboard must not compare this new benchmark against the old prose-reference scores.

Removed: accepted-answer grading prompt, substance score, all-returned-items success logic, silent
10k-character grading truncation, automatic historical-case promotion, and an obsolete test that
expected an undeclared judge-created requirement to pass. New complete-coverage tests replace it.

## Measured evaluator comparison

Three controlled artifact states shared the same confident reply: correct file, wrong total, missing
file. The pre-change evaluator and its actual Haiku judge passed all three; only one decision was
correct. The new evaluator passed only the correct artifact: all three decisions were correct.

| Measurement | Before | After |
|---|---:|---:|
| Correct pass/fail decisions | 1 / 3 | 3 / 3 |
| Judge calls | 3 | 0 |
| Judge input tokens | 2,541 | 0 |
| Judge output tokens | 796 | 0 |

This paired replay measures evaluator discrimination and grading cost. Candidate generation was held
fixed; it does not establish better agent reasoning or reduced generation cost. Raw synthetic results:
docs/evaluation-paired-comparison.json. The old evaluator is available in the parent Git revision.

## Live baseline after release 544b17d

All four agents completed a graceful restart after notice. The benchmark runs through their normal
worker/provider route. One attempt per case is a small baseline, not a reliability estimate.

| Agent | Cases passed | Requirements passed | Input tokens | Cache-read portion | Output tokens |
|---|---:|---:|---:|---:|---:|
| Cal | 2/2 | 8/8 | 55,238 | 0 | 513 |
| Vit | 2/2 | 8/8 | 87,001 | 69,420 | 1,296 |
| Tacitus | 1/2 | 4/8 | 86,908 | 56,221 | 2,120 |
| Cato | 2/2 | 8/8 | 162,156 | 122,205 | 1,335 |

Cache-read tokens are included in input totals, not additional. Cal used OpenAI gpt-6.1-sol;
Cato, Vit and Tacitus used Claude SDK claude-opus-5-5, all on subscription. Grading used zero model calls
and zero tokens. These are recorded token counts, not percentages of subscription quota.
Tacitus's invoice task stopped in needs_answer: all four requirements are unverified. Its source
correction task passed. No private memory or conversation was inspected to explain that stop.
Cato's benchmark was initially queued behind legacy evaluation task 314. the owner authorized cancelling
that obsolete run: its scheduling driver was stopped and its worker cancelled through TaskStore.control.
Task 314 reached cancelled; benchmark tasks 315 and 317 finished and passed. No agent restart was needed.
All eight baseline attempts are now recorded. Raw completed runs: evaluation-live-baseline.json.

Generation token savings have not been established: these are the first comparable live baselines.
The measured before/after savings above apply only to grading. Normal role context is substantial
relative to these tiny tasks; reducing that context would be a separate change requiring quality checks.


## Lesson-change screening (2026-10-25)

`lesson trial` now accepts `development_cases` and `heldout_cases` (disjoint IDs from
src/tabularium/learning/benchmark_cases.json), `repetitions` (default 2, range 1–5), and `max_steps`
(default 12, range 1–60). The old `task_id` replay and opinion-based comparison are retired.
Eight runs are scheduled for two cases at the defaults: two skills × two cases × two repeats.
This costs more generation than a two-run anecdote; it is explicit, not scheduled on every task.
The extra comparison model call is removed. Artifact grading uses zero model tokens.

An author must supply independently reviewed, relevant cases in the existing versioned benchmark.
Collected historical answers are never promoted automatically. The existing tiny invoice/source
cases test mechanics, not the quality of a research or basket skill. Add relevant cases before using
this to support such a change. Inputs belong in request/context; expected answers stay in the oracle.
No live API benchmark was run as part of this implementation, so generation savings are not established.

The proposal, baseline skill, exact applied candidate (including ledger tag/date and version), seed
and case set are frozen before dispatch. Standing methods are replaced in the seed, not duplicated.
Both splits evaluate the frozen candidate; human revisions require another proposal and fresh
reserved cases. Exposure tracking is per lesson and keyed by input content, not case names or rubric.
It does not establish that a public benchmark was never seen in model training or another project.
Small samples are screening evidence, not statistically demonstrated general improvement or a full
implementation of GEPA's candidate search. Independent subjective judgments remain unverified here.

Trial workers have read_file/write_file only. Reads are confined to the frozen skill and own outputs;
writes to own outputs. Live memory and live task context are not injected. No shell, browser, sending,
delegation, checkpoints that create lessons, or model-controlled checking. This is tool-boundary
isolation, not an OS sandbox. Tasks requiring those tools must be adapted to supplied offline inputs
or evaluated elsewhere; these runs cannot establish performance on live browsing or shell work.

One provider session per attempt has the same step allowance. This is not a token ceiling. Reported
usage is kept separate from estimates; route mismatches or unknown usage prohibit token-saving claims.
The monitor grades files only once workers settle, includes crashes, and never automatically retries
failed trial attempts. Normal crash alerts remain intact. A comparison delivery failure is retried;
as with the existing task notifier, a crash after sending but before acknowledgement can duplicate it.
No claim of exactly-once external delivery is made. Incomplete queue creation is inconclusive and
cannot satisfy the approval gate. A process killed during queue creation may require operator cleanup
of the incomplete trial; it cannot become successful evidence.

Reports and full per-requirement/usage evidence are saved in the trial folder. Old trial evidence cannot
approve a new edit; a running proposal cannot be replaced; approval still requires the owner's recorded
words. An explicit owner-authorized skip remains available. A failed screening can still be consciously
approved after review: this mechanism supplies evidence rather than replacing human judgment.

Research basis: GEPA (https://arxiv.org/abs/2507.19457), separating proposal/reflection from evaluation;
AI Agents That Matter (https://arxiv.org/abs/2407.01502), cost, accuracy and held-out evaluation.
The particular default sample sizes and offline tool scope are engineering choices, not paper prescriptions.


## Research cases (2026-10-25)

The shared corpus now has six cases. The four additions test research behaviours using fictional,
closed-book source packets, with expected results worked independently of the evaluated agent:

| Case | What counts as evidence | What a plausible bad answer gets wrong |
|---|---|---|
| research-scope-v1 | Both JSON baskets, exclusions, supporting source IDs, and actual CSV rows | Delivers only the original basket or omits it from the spreadsheet |
| research-sources-v1 | Amended annual figure, correct source, same-period growth | Uses the original number or a newer but incompatible quarter |
| research-unknowns-v1 | Provider counters distinguished from missing values and estimates | Invents zero cache use or subscription-quota savings |
| research-decision-v1 | Observed trade-offs plus explicitly unproven deployment suitability | Treats ten easy successes as a representative reliability guarantee |

The scope and unknowns cases are fictionalized from the documented carrier-basket and cache-accounting
failures. The source-conflict and decision cases are designed stress tests, not newly alleged incidents.
These cases do not reproduce live mid-session steering, real web search, comprehensive market research,
or the reliability of a deployed system. CSV checks parse actual rows; quoting and platform line endings
are not research errors. Expected row order and columns are specified in the request.

Twenty-one objective requirements inspect delivered files. A separate decision-usefulness requirement
has an independent rubric: explain the trade-off, qualify the evidence, propose a concrete representative
test and identify the decision criterion. Correct facts alone leave that item unverified without a reviewer.
A model review is labelled subjective and is not proof that the user finds the advice useful. In lesson
trials it remains unverified for owner review. Normal benchmark eval uses its existing structured model
judge for that one item, recording judge usage separately; it does not add a reviewer to every live task.

Cost: full benchmark generation increases from two to six cases per repetition; use the existing
--sample 2 for the original cheap smoke test. Objective grading takes zero model calls/tokens. The full
benchmark can now use one subjective judge call per repetition. No live model runs were performed for
this change and no production-quality or generation-token improvement is claimed. Old and new totals
must not be compared as if they measured the same tasks; fingerprints and evaluator version 3 separate them.
The ordinary verify_requirement path remains the owner of production proof; no second completion gate
or checklist was introduced. Declare checks appropriate to the actual task, bind source files as check
dependencies, and retain subjective review as judgment. Passing this benchmark does not certify every
future answer or the correctness of arbitrary model-written checks.

Validation: independently worked good fixtures, each deliberately corrupted factual field, a missing
basket, an incomplete spreadsheet, missing deliverables, equivalent CSV encodings, and the distinction
between fact checks and subjective usefulness. Missing fields stay unverified and wrong facts fail,
regardless of a confident final answer. The benchmark mechanics are tested without paid model calls.

Benchmark generation now records its result without grading it twice. It must still declare a complete
result without an unresolved question; otherwise the attempt is incomplete. A recorded generation is
not verified quality. The existing evaluator alone inspects its artifacts and, when configured, requests
the structured subjective review. This removes the worker-side grade(None) call that previously prevented
a subjective case from ever reaching the judge. Ordinary completion checks are unchanged.

## Interactive workflow evaluation (2026-10-26)

`tabularium eval cato --interactions --k 2` uses the same regression
report, independent artifact grader and actual-usage accounting as the static benchmark.
It adds controlled events while the real model is working. It does not install a new
service, run a model judge, change live task completion, or run on every conversation.
Use `--sample 1` for the first (steering) case. Cal, Vit and Tacitus have the three
common cases; Cato also has delegation because only his role exposes that tool.

The four cases check:

- A scope update arrives after the first file write. The final file includes both
  requirements and the runtime has actually delivered the update.
- The agent prepares a receipt, asks a question and stops. Its process exits. A new
  process loads the saved question and conversation, records the synthetic user's
  answer through the normal durable-decision path, and finishes without repeating
  the receipt write. This checks process reconstruction, not supervisor crash recovery
  or Telegram delivery.
- A genuine corrective follow-up and a runtime warning arrive at a tool boundary.
  The correct user message is recorded as feedback, the file is corrected, and the
  warning is absent from saved conversation history. A message arriving during the
  last tool is also processed when it becomes the next foreground turn. Tacitus intentionally does not
  collect correction-learning records; his file/follow-up/history checks still apply.
- Cato receives a deliberately incomplete delegate delivery with one already-proven
  item. He requests targeted rework and accepts the corrected delivery while preserving
  the unchanged proof. The delegate is a deterministic environment actor, not another
  model. The real delegate tool, agreement, acceptance and proof stores are exercised.
  Requesting rework is a valid rejection; an extra accept(not_met) call is not required.
  If Cato parks, the existing delegate-return/wake path queues his continuation in a new process.

Each attempt copies only the role entry-point code and its small Python dependencies
into a disposable folder. Identity/style are synthetic; live skills, memory, history,
checkpoints and task queues are not copied or read. Read/write tools are fenced to
that attempt's outputs. Shell, messaging and new delegation are unavailable. The
model cannot write the trusted observations; the parent publishes those after the
child exits and overwrites any imitation left in the output folder.

The first configured provider/model is frozen for the run, with no automatic billing
fallback. Each process has 16 model/tool iterations and a 600-second deadline; deadline
cleanup terminates only that attempt's process tree. Decision and parked-delegation cases use two processes.
Usage is recorded across both. Missing provider usage remains unknown, including the
Claude question boundary when the SDK supplies no terminal usage frame. Estimates are
not substituted. Reports record the harness hash, route and limits; comparisons refuse
different execution identities as well as different cases, oracles and repetition counts.

The method is inspired by [tau-bench](https://arxiv.org/abs/2406.12045) and
[tau2-bench](https://arxiv.org/abs/2506.07982): interactive environments, independent
state checks and repeated attempts. These are small deterministic local scenarios,
not a reproduction of either paper's benchmark or evidence of general agent reliability.
The synthetic success criteria were authored and reviewed during implementation;
they are not an independently maintained external benchmark.

### Coverage and cost comparison

Four negative-control runs deliberately leave correct-looking final artifacts while
omitting the scope event, guessing the unanswered choice, attributing feedback to the
wrong message, or accepting the bad delegate delivery before repairing it. Artifact-only
checks pass all four; the interaction checks reject all four. Both graders use zero
model calls. This is a controlled coverage comparison, not a claim that the old six-case
benchmark itself ran these scenarios.

Production agent behavior is unchanged by this upgrade. Therefore there is no claimed
before/after reasoning-quality improvement or token saving. Real-model runs establish
a baseline for evaluating future framework changes on matching scenarios. They consume
subscription tokens explicitly; the ordinary workflow gains no extra model calls.
Development runs exposed an ambiguous JSON request and an overly prescriptive rejection
oracle. Those were corrected before the final version-3 baseline; development scores
must not be compared with the frozen baseline.

### Final measured baseline and development cost

The version-3 harness hash matches both final saved runs. Each scenario was attempted
twice. Cato passed 8/8 attempts (four scenarios); Cal passed 6/6 (three common scenarios).
All final requirement checks passed. This small sample does not establish general
reliability. Earlier Cal runs produced correct files but left a declared check unverified
or omitted the completed outcome; this evaluator change does not claim to fix those
intermittent behaviors.

| Final run | Input tokens | Cached input (included in input) | Output tokens |
|---|---:|---:|---:|
| Cal, OpenAI subscription | 200,132 | 94,336 | 5,834 |
| Cato, Claude subscription, known subtotal only | 512,412 | 480,881 | 10,646 |

Cato's two question-boundary responses have unknown usage, so his full totals remain
unknown. Grading used zero model tokens. Raw input/cache figures are not subscription
quota percentages and are not comparable billing costs across providers.

Development and final runs together comprised 25 Cato attempts and 15 Cal attempts.
Cal's total was 465,105 input (140,672 cached) and 12,886 output tokens. Cato's known
subtotal was 1,387,611 input (1,284,551 cached) and 29,018 output; six responses had
unknown usage. These include the harness-development runs, not just successful runs.

Local evidence: cato/task_state/regression/20261026-095610.json and
callimachus/task_state/regression/20261026-095914.json. The reports retain attempt paths,
synthetic transcripts, observations and usage. Validation: 477 shared tests and 90 Cato
tests passed. One earlier full-suite run hit a Windows SQLite cleanup race in an outbox
test; the isolated outbox suite and subsequent full suite passed. Nine focused interaction
tests passed after the final synthetic-email fixture adjustment. Public export passed.

No live agent restart was performed. The new operator command is usable immediately;
loaded dashboard code receives the comparison guard at its next normal release.

## Completion feedback, 2026-10-26

The earlier Cal runs exposed a timing error: completion checked the saved evidence only
once the model could no longer act. The same decision now also runs before a provider
accepts a worker's final response. Its preview cannot finish, park or ask on behalf of
the task. The final commit rechecks current proof and durable controls to catch changes
since the preview. Both call the same implementation; no second checker or model judge.
Existing terminal-only acceptance is replaced with feedback through the existing API
loop / Claude SDK retry path. No production file, service, table or task state was added.

Only an active worker's apparently final draft is eligible. Explicit blockers, questions,
waiting tasks, runtime interruptions, and successful or record-only runs do not get an
extra model turn. A repeated set of unresolved reasons stops repair; at most two repair
rounds can be requested in a turn, including across session limits. The final task
still fails completion if evidence remains missing. Diagnostics and rejected drafts use
the existing runtime-event record and wire-only feedback, never saved user messages or
prefixes on the delivered reply. Valid verification receipts remain valid and unchanged.

This applies CRITIC's external-feedback principle (ICLR 2024,
https://arxiv.org/abs/2305.11738), consistent with the evidence distinction in Kamoi et al.'s
TACL 2024 survey (https://arxiv.org/abs/2406.01297). Neither paper prescribes this exact
lifecycle or retry allowance, nor guarantees improvements here.

### Verification and measurement

The new missing-proof regression failed on the old path: a correct saved file ended
needs_attention. It now finishes after a real verify_requirement call through all four
role classes on OpenAI, Anthropic API and Claude subscription. Seven new tests also
cover missing outcome declarations, failed/stale proof, preservation of unrelated proof,
no extra calls on ordinary success, genuine waits/blockers, side-effect-free state
preview, changed files before commit, repeated/changing failure limits, and session
continuation preserving the repair allowance. Two old success fixtures were updated to
declare completed outcomes and their subjective requirements; their original tool-call
and usage expectations remain unchanged. Full suites: 484 shared and 90 Cato pass.
Public export: 225 files passed. No memory changes or live task edits.

The frozen version-3 interactive scenarios, routes, effort settings, harness hash and
k=2 match the preceding baseline. Cato again passed 8/8 and Cal 6/6, with all requirements
passing and zero grading model calls. This is a small regression sample, not a general
reliability estimate. Cal actually needed the new completion feedback in three attempts:
one changed-instruction task and both decision-resume tasks omitted task_outcome=completed.
Each recovered with one final response and no further tool calls; without this feedback,
that specific draft would have stopped as needs_attention. Cato needed no completion
repair. Controlled tests separately demonstrate repair of missing and failed checks.

| Run | Input tokens | Cached input (included) | Output tokens |
|---|---:|---:|---:|
| Cal before | 200,132 | 94,336 | 5,834 |
| Cal after | 205,922 | 90,496 | 5,245 |
| Cato before, known subtotal only | 512,412 | 480,881 | 10,646 |
| Cato after, known subtotal only | 492,482 | 438,624 | 10,811 |

Cal input increased 2.9% and output decreased 10.1%; this is not evidence of token
savings. The three repair responses themselves consumed 18,838 input (4,096 cached) and
257 output tokens. The smaller net input increase reflects variation in other work.
Successful finals add no model call or feedback text. Cato has two unknown-usage
question-boundary responses in each run, so full usage and a full usage delta remain
unknown. His existing foreground unanswered-message retry ran twice instead of once;
that unrelated behavior and cache variation must not be attributed to completion repair.
Raw provider tokens are not subscription quota percentages.

Evidence: cato/task_state/regression/20261026-123945.json and
callimachus/task_state/regression/20261026-124149.json, compared with the baseline paths
above. These fourteen attempts are all live-model calls made for this change. Runtime
code has not been restarted; deployment requires the next release or explicit exception.

## Reply coverage by message identity, 2026-10-26

The completion benchmark exposed a separate foreground problem: Cato returned a correct
revised file, but the runtime rejected the answer for not declaring the original message
through copied opening words. Those words were a fragile identity protocol: a common
substring could also accidentally match several unrelated messages.

The existing reply.answers field now contains integer message IDs resolved against the
current turn's existing inbox records. It serves both coverage feedback and reply quoting.
One reply may acknowledge the original request and its correction; unrelated requests
still need their own IDs. Deleted the text normalizer and substring matcher. No new
store, state, tool, model judge or compatibility-guessing path. Old strings, unknown IDs,
booleans, and non-integer IDs do not claim current messages. The schema explains the
meaning of the replacement field; existing incoming-ID context is reused unchanged.
Acknowledgement is still a model declaration, not evidence of substantive correctness;
verification of actual deliverables is unchanged. The existing single retry for missing
coverage is retained, so this is not a guarantee that a model will answer every request
correctly after feedback.

This targets the verification/coordination failure class described by MAST
(https://arxiv.org/abs/2503.13657). MAST does not validate this specific ID protocol; the
local regressions and measurements below are its engineering evidence.

Ten focused tests include exact coverage, duplicate message text, legacy/unknown IDs,
quoting the right message, a revised request plus an unrelated request, and missing
artifacts remaining incomplete despite full acknowledgement. Real role objects and all
three provider paths exercise missing-ID feedback; runtime warnings remain absent from
saved human messages. Existing follow-up, merged-message, correction-attribution and
interactive tests now use IDs. Seven assertions failed before the implementation;
full suites afterwards: 490 shared + 90 Cato pass. Original file line endings preserved.

Only the affected feedback scenario was rerun live, twice per provider (four attempts),
using the same frozen version-3 case, route, harness hash and effort settings. Compare
these per-case attempts with the corresponding case in the preceding full reports,
not the full-report aggregate scores: different selected case sets are not comparable.
Both agents passed 2/2 before and after; all five requirements passed in each attempt,
including the revised file and correct human-feedback attribution. Zero grading calls.

| Agent / affected case | Input before / after | Cached input before / after | Output before / after | Coverage retries before / after |
|---|---:|---:|---:|---:|
| Cato | 103,617 / 93,213 | 91,169 / 78,466 | 2,518 / 1,746 | 2 / 0 |
| Cal | 37,644 / 37,690 | 20,608 / 4,224 | 616 / 585 | 0 / 0 |

All usage in these selected attempts is provider-reported and known. Cato's input fell
10.0% and output 30.7%; Cal's input rose 0.1% and output fell 5.0%. Cato's SDK sessions
fell from six to three, including variation in whether a follow-up required another turn;
not all of that difference can be attributed to the removed coverage retries. Cal's
uncached input rose from 17,036 to 33,466 because fewer inputs hit cache. This small
sample demonstrates removal of the observed false retries, not a reliable subscription
quota or general cost saving. The revised full reply schema estimates 812 tokens versus
795 previously with o200k_base (+17); this estimate is separate from actual usage and
is not a Claude tokenizer measurement. No additional model call is introduced by design.

Evidence: cato/task_state/regression/20261026-133049.json and
callimachus/task_state/regression/20261026-133115.json, against the interaction-feedback
attempts in 20261026-123945.json and 20261026-124149.json respectively. No live memories,
user tasks, checkpoints or routing settings changed. Deployment awaits a restart.


## Durable subscription usage (2026-10-26)

Claude usage previously depended on receiving the final SDK result. A decision pause,
connection failure or process crash could discard input/cache counters already reported
by the provider. The shared subscription runtime now saves those counters before handing
each message to the turn consumer. One existing usage_events row per SDK query is updated
in place, then reconciled with final usage. Message IDs deduplicate repeated assistant
messages; child messages are excluded to match the top-level result's main-loop scope.
No new table, model call, task state or accounting service is introduced.

The SDK's documented per-step output_tokens is a placeholder, so it is never counted.
Interrupted sessions retain known input/cache subtotals and unknown full totals/output.
A zeroed crash summary cannot erase previously reported input. The existing usage summary
and heuristic effort score include known subtotals without claiming complete measurement.
Snapshot timestamps reflect the latest observation; spend orders observations by timestamp
and ID so a final snapshot clears preceding tool events despite retaining its original ID.
Historical measurements are not rewritten.

Provider contract: https://code.claude.com/docs/en/agent-sdk/cost-tracking
Academic motivation: AI Agents That Matter, https://arxiv.org/abs/2407.01502, argues for
joint cost/quality evaluation; it does not establish this implementation's correctness.

The old-code interrupted-session test lost all 130 reported input tokens. The replacement
retains 130, then a final summary reconciles to 130 input / 50 output in one row. Tests
exercise all four role runtimes, duplicate messages, child messages, absent IDs, zeroed
summaries, connection exceptions, real decision waiting/cancellation and a child process
exiting abruptly after a reported message. Another regression caught pending_tools staying
at one after a final snapshot; ordering by latest observation fixes it. Ten focused tests
pass. OpenAI's existing per-exchange accounting is unchanged and covered by the full suites.

One live isolated Cato interaction-decision attempt passed all five independent checks:
prepare once, ask, stop, resume in a new process and produce GBP without repeating the
preparation. Report: cato/task_state/regression/20261026-161213.json. Its paused query now
retains 9,850 input tokens (2 uncached, 9,848 cache writes) that the old final-only path
would discard. Output for that query remains unknown. The resumed query reports 21,847
input and 465 output. Combined known input is 31,697 (6 uncached, 19,289 cache reads,
12,402 cache writes); known output is 465, not a complete output total. No grader calls.
The live run exposed the ordering bug above; its saved report still has pending_tools=1,
and is retained unchanged. The subsequent regression proves the ordering correction.

This is improved measurement, not lower consumption or better output quality. There is
no matched live before/after cost claim, no quota estimate, and no attempt to reconstruct
unreported historical usage. Accounting remains best-effort if storage itself fails.

Release verification: 493 shared + 90 Cato tests passed via the normal restart gate.
All four agents restarted and have fresh matching health and Telegram polling.
The separate running dashboard process was not restarted; its old aggregate reader
still omits partial subtotals until its next restart. Persisted accounting and the
agents' usage summaries use the replacement now.
