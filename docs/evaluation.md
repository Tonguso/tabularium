# Evaluation redesign, 2026-10-23

The old evaluator compared a reply with an earlier agent reply, let the judge choose which plan
items to return, and treated any nonempty all-true subset as a pass. It never opened the deliverable.

The shared evaluator now owns the requirement set. Every ID receives passed, failed or unverified.
A missing, repeated, invented or malformed subjective verdict invalidates that judgment batch.
Objective requirements inspect the delivered JSON field, text file or workbook cell, recording the
path and SHA-256 of the bytes actually checked. Wrong data fails; unreadable/missing data is unverified.
A confident reply is not a substitute. Subjective rubric judgments remain explicitly labelled.

Default cases live in agent_core/benchmark_cases.json, with version, independent expected values and
oracle provenance. The two synthetic cases cover four invoice checks and four source/correction checks.
They apply to all four roles. Codex checked their arithmetic/source facts; they are not claimed to be
the owner-approved answers or a comprehensive measure of research, coding or private conversation quality.

Historical evals/cases.json files are preserved as candidates only. --collect collects candidates and
exits; it never promotes agent prose to ground truth. Tacitus's private history cannot be collected.
Benchmark worker tools are limited to reading/writing local files, checkpoints and verification;
no shell, web, sends, memory edits or delegation. These restrictions apply to these closed-book
synthetic cases, not ordinary agents or lesson trials. The normal agent context may still affect a run;
this benchmark is not a test of memory isolation or of every production tool.

Run `python agent_control.py eval <agent> --k 1` for a first baseline, then repeated runs (--k 3) when
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
