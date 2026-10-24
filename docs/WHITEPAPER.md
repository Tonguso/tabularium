# Tabularium: An Owner-Judged Agent Framework

Version 0.1, 24 October 2026

## Abstract

Tabularium is a framework for running a small cast of AI agents on behalf of one person. It was built by that person
with two coding agents over four weeks of daily use, and it is published here as working code with about five hundred
tests. Its organising principle is simple to state and turns out to be hard to keep: no part of the system believes
what another part says about its own work. Where a conventional agent harness puts a rule in a prompt and trusts the
model to follow it, Tabularium writes a record and has the runtime check it: a task that was started, a question that
was asked, an answer that was given, a file that was delivered, a check that ran. The owner is the only judge of
quality; the records are the only authority on fact. This paper describes the design, the mechanisms that implement
it, the failures that produced each one, and what the approach cannot do.

## 1. Introduction

Most agent frameworks are built around the model: a loop that calls it, runs the tools it asks for, and returns its
last message. The framework's job is to make that loop convenient. The approach works for demonstrations and breaks
in daily use, for a reason that has nothing to do with model quality. A model produces text, and text can say
anything. "I have started the research" is one sentence whether or not a job exists. "Done" is one word whether or
not the file was written. A rule in the prompt ("never say done unless the file exists") is more text, and the model
follows it most of the time, which in daily use means it fails every few days.

Tabularium started as a chief-of-staff agent on Telegram for one owner and grew, failure by failure, into a framework
whose every mechanism answers the same question: what record, that code can read, says this actually happened? The
owner's complaints drove the design. An agent said work was running when it was not; the fix was a record of started
work that the runtime checks before a reply is sent. An agent asked the same question twice because the first answer
arrived while it was busy; the fix was a durable decision that a reply binds to. A build was accepted three times on
its builder's reports and crashed on first use; the fix was a declared check that runs on the delivered bytes and a
verdict that says how the reviewer knows.

The result is a system that is unusual in three ways. It is **owner-judged**: agents propose, trial and diagnose, but
nothing about how they work changes without the owner's own words. It is **record-first**: every claim, question,
confirmation, requirement and delivery is a row or a file before it is a sentence. And it is **operated like
software**: self-tests, a supervisor, release gating, scorecards, traces, incident reviews, and two independent coding
agents that change the code and check each other's work.

## 2. Design principles

Six rules explain almost every part of the system, and a proposed change is checked against them before it is made.

1. **The owner is the only judge.** Agents may notice a mistake, diagnose it and propose a fix, and the system tests
   the fix, but no rule changes without the owner's words.
2. **Records, not memory.** Anything that must survive a turn, a crash or a restart is written to a database or a
   file before it is acted on. The model's context is a view of the records, never the source of truth.
3. **Declared, not inferred.** Where the runtime needs to know something about a reply (what it answers, whether it
   started work, whether it was a correction), the reply declares it in a structured field and the runtime checks the
   declaration against the records. Nothing is inferred by reading prose.
4. **Nobody's word for their own work.** A worker's "done", a delegate's report, a model's grade of a model's answer:
   each is an assessment, stored as such, never a verdict. Verdicts come from checks the runtime ran.
5. **The code says no.** Rules the model could skip are enforced in code, and the refusal is visible: an undelivered
   reply is sent back, a blocked delivery says why, an unproven item is named to the owner as unproven.
6. **Fix the design, not the symptom.** A failure is a decision that allowed it. The fix removes the decision for
   every path that shares it, with a test that failed before. Patches, retries and special cases are refused.

## 3. Architecture

### 3.1 The cast

Four agents share one engine and have separate identities, skills, memory, folders and Telegram bots. They never share
a conversation and never read each other's files.

| Agent | Role | Delegates to | Notes |
|---|---|---|---|
| Coordinator | Research, analysis, coordination of the others | Librarian, engineer | Owns briefs, acceptance and the report to the owner |
| Librarian | Evidence gathering, knowledge records | Nobody | Two parallel workers; runs under the caller's budget |
| Engineer | Implementation on the owner's machine | Nobody | Single worker; builds and tests artifacts |
| Private agent | Personal matters, in confidence | Nobody | No shell, no delegation, no learning loop; records closed to maintainers |

The roster is one table in code. A role declares whether the agent delegates, how many workers it runs, whether the
owner's corrections start a diagnosis, and which tools it may hold. Adding an agent is adding a row.

### 3.2 The machine

Six processes run on one PC: a supervisor, the four agents, and a dashboard. The supervisor starts the others,
adopts them after its own restart, watches heartbeats, and restarts a crashed process with a crash-loop guard; a
planned exit is acknowledged and never counted as a crash. Each agent is one Python process holding a Telegram
connection, an inbox, and the shared engine. Long work runs in a separate worker process per task, with a lease that
survives manager restarts. The dashboard is a local web page that reads the records; it never commands an agent.

| Part | What it is |
|---|---|
| Inbox | A small database per chat: waiting messages, follow-ups, updates to running jobs, usage events |
| Engine | Shared code: the turn loop, the tool dispatcher, the reply envelope, the safety rails |
| Task store | SQLite: tasks, their lifecycle, decisions, controls, delegations, requirements, proof, activations |
| Outbox | Every outgoing message, written before it is sent, retried in order after an outage |
| Memory | One text file per agent plus a searchable archive of every conversation |
| Skills | Folders with a written method (`SKILL.md`) the agent loads when a task matches |
| Checks | A daily self-test, a self-test after every model-client update, weekly memory exam and review |

### 3.3 One message, start to finish

Every message becomes one turn: the model reads the message and the current records, uses tools, and returns one
reply. The path is the same for all four agents.

1. The owner sends text, a voice note, a document or a photo. Voice is transcribed; a voice message gets a spoken
   reply.
2. The inbox records the message durably, with its Telegram id. A message that arrives while a turn is running is
   queued, and the running turn is told about it at the next tool result, not after it finishes.
3. The request is built from the saved history plus what is true now, attached to the latest message on the wire
   copy only: the core memory read fresh from the file, this turn's notes, live task status scoped to the request,
   pending questions, and a runtime note if the owner has corrected the agent twice in the current thread.
4. The model works: tool calls are executed by the runtime, and each one leaves a receipt on disk before and after
   it runs. Tool outputs above a size are archived to files the model can read back, not kept in context.
5. The model returns a reply envelope: the reply text, what it claims (work started, question asked, request
   handled), which of the owner's messages it answers, and whether the owner just corrected it.
6. The runtime checks the claims against the records. A promise of work with no job started, a reply that leaves
   out one of the owner's messages, a status that contradicts the task store: the reply is sent back to the model
   with the discrepancy, once.
7. The reply goes to the outbox and on to Telegram, quoting the message it answers only when the owner has sent
   something else since. If it asked a question, the Telegram message id is recorded on the decision.

A background task is the same loop in a worker process, with checkpoints, a lease, requirements and a delivery gate.

## 4. Records

The task store is the heart of the framework. Its tables, and what each one makes impossible, are the clearest
description of the design.

### 4.1 Lifecycle

A task's status changes only through named operations in one table: claim, run, park, wake, ask, answer, finish,
stop, recover, resume, reopen, cancel. Each names the states it may start from and the states it may lead to; a move
outside the table raises and writes nothing, and a move only succeeds if the task is still in the state the caller
read. A question about a finished task cannot silently reopen it; reopening is its own operation. Before this table
existed, task status was written by thirteen statements in two files.

### 4.2 Decisions

Everything an agent asks the owner is a decision: one durable row with the question, the task, the Telegram message
that asked it, and later the answer, its source message and the owner's quoted words. Two kinds exist. A question
("30 names or 50?") is answered by the owner's next message, or by a reply to the question's message, which binds
directly. A parked action (send this email, run this command) stores the exact inputs and runs only on a yes, whenever
it comes; a late yes is still a yes, and a restart forgets nothing. One pending question per task is enforced by a
unique index. Stale decisions are annotated when the task finished, was superseded, or the topic moved on.

### 4.3 Requirements and proof

A task carries its obligations with it. The brief and every later instruction become numbered requirements in the
database; a later checklist can add or revise, but omission never removes one. Each requirement can declare a check:
a command to run against the delivered file, or a literal the file must contain. The runtime runs the check on the
exact bytes (recorded by hash), stores the observed output as proof bound to those bytes and that requirement, and
derives the status: passed, failed, or unverified. A changed file, a changed test, a rework or an interrupted check
makes the proof stale. Model prose about a requirement is stored as an assessment, never as proof.

### 4.4 Completion

"Finished" is derived, not declared. A task finishes only when every requirement has current passing proof, no
question to the owner is pending, no instruction is undelivered, and every delegated piece has been accepted. A
worker's completed envelope with any of those missing ends in "needs attention" with a record of what is missing.
The earlier design ran three self-review retries on an unchanged draft and accepted "done" with a clear blocker; both
were deleted when this rule replaced them.

### 4.5 Receipts, checkpoints, activations, release

Every tool call writes a receipt before it runs and the outcome after, so a crash leaves the action's status known
rather than guessed, and a worker resuming from a crash inspects receipts before repeating an external action.
Workers checkpoint progress, facts, assumptions and decisions. An activation row records every start, resume and
reopen, and backs the reply envelope's "work started" claim. Each task records the commit of the code it ran on.

### 4.6 Outbox

Every message to the owner is written to a per-agent outbox before the send is attempted. A network failure or a
"not now" from Telegram leaves it queued; a retry thread delivers in order, dropping the connection pool before
retrying, and a chunk delivered more than two minutes late says when it was written. A stall shows on the dashboard
within a minute. A rejection by Telegram itself falls back to no quote, then plain text, and is marked failed only
after that.

## 5. Working together

### 5.1 Delegation by contract

Agents do not talk to each other. The coordinator writes a brief, because an AI writes a better prompt than a person,
and the brief is a contract: numbered completion items with the critical ones first, how each will be verified, an
early judgment slice the delegate finishes first, a budget target the delegate sizes its work by (never a stop), and
a completion condition. The request is recorded against the coordinator's task. The coordinator can read the
delegate's checkpoint, answer a question it stopped on, rework named items, or rescope by adding obligations; it
cannot silently remove one.

### 5.2 Waiting without a slot

When a coordinator's task is waiting on a delegate, it parks: the worker slot is freed, the runtime polls the delegate
without model calls, and the task wakes when the result returns or the delegate asks something. A hand-off made from
a conversation starts the delegate at once and creates the receiving task already parked, so the hand-off never queues
behind unrelated work.

### 5.3 Acceptance and the owner's view

Delivery is blocked until every attached result is read and accepted. Acceptance is one verdict per item, each saying
how the reviewer knows: ran (executed, launched or opened it), read (inspected its content or output), or report (the
delegate's word). A ran or read claim with a declared check derives its status from the check; report-only items stay
unverified and are named to the owner as not run by the reviewer. The delegate's own result is kept as fetched, and its
DONE, OPEN, EVIDENCE and COST lines are appended unedited beneath the coordinator's verdict, so the owner reads what the
delegate claimed and what it left open. A scorecard records, per request, what was asked, delivered, verified how,
at what cost, with how many reworks.

### 5.4 Privacy by role

The private agent has no shell, cannot delegate, holds the smallest tool set, and its memory and conversations are
never read by the maintainers or the other agents; only its tool records may be inspected. The rule is enforced in
code and in the contribution rules, and the operations log notes compliance on every change that could touch it.

## 6. Talking to the owner

### 6.1 Asking

Questions and confirmations are decisions (section 4.2). The reply that asks carries the decision id, so the owner's
reply to that message answers it directly, with no search through history. The owner's answer arriving while the
agent is busy is recorded and delivered to the worker as a control at its next safe point.

### 6.2 Waiting on an answer

A worker that asks the owner something ends its turn at the next settled action boundary: the current tool result is
saved, nothing further is started, the slot is freed, and the answer queues the continuation. Before this rule a
worker kept executing for minutes after asking, holding the slot while the next job waited.

### 6.3 Corrections

Every reply declares whether the owner just corrected the agent. Corrections collect, and once the thread has been
quiet for ten minutes one background diagnosis runs for the whole thread: what the agent did, what the owner expected,
why, and whose fault it was. A fault in the agent's method becomes a candidate lesson, with an exact skill edit
registered only when one sharpened rule would stop a recurrence. A fault in the runtime becomes a report for the
coding agents. The diagnosis never speaks to the owner and cannot ask him anything; he reads the digest when he
chooses. After two corrections in a thread, the agent's next reply may not fix, compute or propose anything: it
restates in the owner's words what it now understands and asks whether that is right.

### 6.4 Quoting and coverage

A reply quotes the message it answers only when the owner has sent something else since; every reply declares which
of the owner's messages in the turn it answers, and a reply that leaves one out is sent back once.

## 7. Memory

An agent's memory is one text file, readable by the owner, plus a searchable archive of every conversation. The
model remembers nothing between turns; the current file is attached fresh to the latest message of every request,
so a correction is in force from the next request and no stale copy can contradict it. One tool writes memory, and a
decision or preference of the owner can be written only with the message cited and the owner's words quoted, which
the runtime checks. Two checks run weekly. A memory exam takes a few durable things the owner said that week (not
instructions about one deliverable), has a separate model turn each into a question, asks the agent with only its
memory to go on, and records the score; it is shown on the dashboard with the missed questions. A memory review
proposes tidy-ups as a candidate copy, which is applied only after the exam passes on both the current and the
candidate version and the owner agrees, and only onto the version it was built from.

## 8. Learning

Agents never quietly rewrite their own rules. Skills are folders of written method, separate from who does the work
and what it costs, so a method can be published without the practice. The learning loop has five states with the
owner between the second and third.

1. **Candidate.** A mistake noticed by the agent or the owner is recorded with the failure and the correction in one
   line. Candidates expire after thirty days unless repeated; a weekly digest groups repeats by theme.
2. **Proposed.** The exact edit to one skill file: the current text quoted, the new text in the skill's voice.
3. **Trial.** A finished past task is rerun twice in isolation, once with the current skill and once with the
   proposed edit; nothing is sent or changed, and the owner receives both answers with a comparison.
4. **Applied.** On the owner's yes in his own words, the runtime applies exactly the proposed edit and bumps the
   skill's version. The agent never edits the file itself for a lesson.
5. **Verified or back.** The next real task where the failure could recur records whether it did. A recurrence
   returns the lesson to candidate.

Each skill carries a track record (when it was last used, by which task, with what outcome) that the agent sees when
it loads one, so "I have used this before" is a fact.

## 9. Safety rails

Each rail exists because something went wrong once. The failure is the specification.

| Rail | The failure it stops | How it works |
|---|---|---|
| Declared work | "I will do X", then nothing happens | A promise with no activation record is sent back to the model |
| Declared answers | Several messages sent, one answered | A reply that leaves out a message is sent back once |
| Typed outcomes | A tool's "no" read as "done" | Refused and Failed are distinct types the code can see |
| Taint | Outside content instructing the agent to send mail or run a command | After reading outside content, sending and destructive commands become parked actions |
| Parked actions | A confirmation lost to a timeout or restart | The exact action is stored and runs only on the owner's yes |
| Task lifecycle | Tasks silently changing state | Named operations checked against one table |
| Status from records | An agent claiming work is running when it is not | Live task status is attached to every turn; contradictions are logged |
| Requirement verification | A build accepted on its builder's report crashed on first use | Declared checks run on the delivered bytes; verdicts say how the reviewer knows |
| Delegate's own account | The coordinator as sole witness | The delegate's head lines travel unedited under the verdict |
| Durable requirements | An instruction lost when a checkpoint rewrote the list | Obligations have stable numbers; omission never removes one |
| One completion decision | A worker's "done" taken at its word | Finished is derived from proof, pending questions and accepted dependencies |
| Waiting on a decision | A worker holding its slot after asking | A pending question ends the turn and frees the slot |
| Correction bundling | Twelve diagnosis tasks from one argument | One silent diagnosis per quiet thread; the next reply restates |
| Durable outbox | A reply lost during an outage | Written to disk first, retried in order, stalls visible |
| Announced routes | Two days on a fallback model unannounced | A missing route is announced; a changed client runs the self-test |
| Truthful live status | A dead process shown idle; planned restarts counted as crashes | Status from process identity; unknowns shown as unknown |
| Lesson approval | Agents changing their own rules | Owner's words only, after a trial |
| Memory candidates | A tidy-up that loses a fact | Exam before and after; applied only onto its base version |
| Checkpoints and receipts | A crash losing work | Every tool call is a receipt; progress is checkpointed |
| Drain restart | Restarting mid-task or onto broken code | Waits for work, runs all tests, refuses uncommitted code |
| Privacy by role | Maintainers reading private conversations | The private agent's records are closed; only tool records |

**Known gaps.** Command screening is a pattern list, not isolation; a real sandbox with desktop integrations behind
narrow host-side tools is the next large piece. A declared check proves that a test ran against those bytes, not that
the test was a good one.

## 10. Operations

Tabularium is run like software, which is uncommon for agent systems.

| Instrument | What it tells you | How often |
|---|---|---|
| Self-test | The agent can take a message, read a file and answer, and which model route answered | Daily, five minutes after every start, and whenever the model client changes |
| Scorecard | The week per agent: turns, bounced replies, corrections, tasks finished, delegated items met, memory exam | Weekly |
| Trace | Everything one task did, in order: tools, model calls, tokens, failures, minutes | On demand |
| Regression benchmark | Fixed synthetic cases with independent answers; code checks the delivered files; pass rate and flaky cases per task type | Weekly, and after any change |
| Research signal | A few real past tasks rerun; a model says only whether each new answer is closer, the same, or further from the accepted one | Weekly; a signal, never a score |
| Live probes | Token use and pass rate before and after an engine change, on matched synthetic tasks | At each release |
| Incident reviews | What went wrong, what each actor could see, which design choice allowed it | After each failure the owner noticed |
| Release record | Which commit each task ran on | Every task |

Restarts drain current work, run both test suites, and refuse uncommitted code. The code has three authors, a human
owner and two coding agents, each recording every change in one operations log and never reverting another's change
until the owner confirms. The two coding agents work concurrently on purpose: each has caught real mistakes of the
other. One release a day and a concept budget (a change that adds a named thing removes one) keep the pace and the
vocabulary in check.

## 11. The owner's role

The system is built so that the human is the judge, not the operator. The jobs nobody else can do:

| Job | When |
|---|---|
| Rule on proposed lessons and trial results | When an agent presents one |
| Approve or decline parked actions | When asked; there is no deadline |
| Approve memory tidy-ups, after seeing the exam before and after | After the weekly review |
| Read the lesson digest and the scorecard | Weekly |
| Say when something is wrong, in any words | Any time; the agent works out the specifics |
| Confirm which past answers were right, so the eval has trustworthy references | Occasionally |
| Authorise changes to the system in terms of what they will change for you | When a coding agent proposes one |

Everything else (diagnosing failures, writing fixes, testing them, restarting the agents, measuring) is done by the
agents and the coding agents.

## 12. Related work

Tabularium borrows from, and departs from, several lines of published work.

- **Reflexion and Voyager** keep verbal lessons and a library of executable skills. Tabularium keeps both, but gates
  promotion behind a trial and the owner's words; ungated libraries fill with the agent's own patches.
- **Generative Agents** consolidate memory by periodic reflection. Tabularium's memory review does the same with an
  exam before and after and the owner's consent.
- **SWE-agent and Manus** argue that the agent-computer interface is the agent's intelligence: receipts, archived
  outputs, refusals of pointless repeats. Tabularium's receipts, archive and loop detectors follow that line.
- **τ-bench** showed that an agent passing a task once passes it only about half the time across repeats, and that
  this reliability is what users feel. Tabularium's benchmark reports pass-every-time and flaky cases per task type.
- **"Why do multi-agent LLM systems fail"** names specification, inter-agent misalignment and verification as the
  failure families. Tabularium's contracts, delegate's account and requirement verification address the second and
  third; the correction rules address the first.
- **Anthropic's building-effective-agents note** advises the simplest pattern that works. Tabularium's four agents
  are justified by trust boundaries (an agent for personal matters) and capability (an engineer with machine access), not by
  throughput.
- **Cognition's argument against multi-agent systems** holds that every hand-off loses context. Tabularium accepts
  the loss and makes it visible: the brief is a contract, the result travels unedited, and the coordinator is not
  the only witness.

## 13. Limitations

- The benchmark is small and synthetic; it proves the evaluator and the plumbing, not research quality. The research
  signal tracks drift but is not a score. A real task becomes a trustworthy case only with a declared check or an
  answer the owner confirmed.
- Declared checks prove execution against bytes, not that the check was the right one. Subjective quality remains a
  judgment, labelled as such.
- The system is single-owner, single-machine, Windows, and addresses the owner by name in prompts. Generalising
  these is open work.
- Model access through vendors' own clients is the operator's responsibility under the vendors' terms.
- Command screening is not a sandbox.

## 14. Lessons from building it

| Date | What went wrong | What changed in the design |
|---|---|---|
| 2026-10-12 | Working notes were delivered as a reply | The reply is a declared field; text outside it is never sent |
| 2026-10-16 | "I will narrow the research", and nothing started; two hours lost | Replies declare whether work started; an empty promise is sent back |
| 2026-10-20 | A second model checking replies appended stale task numbers to a correct answer | The checker was removed; status comes from records; nothing machine-written is added to chat |
| 2026-10-20 | The owner's answer arrived while the agent was busy and the question was asked again | Questions carry their message; a reply to it answers the decision |
| 2026-10-21 | Three messages in one turn; the reply answered one, quoted another, dropped the third | Replies declare which messages they answer; uncovered messages send the reply back |
| 2026-10-21 | Confirmations waited 60 seconds in memory; a late yes counted as no | Risky actions are parked as durable decisions with exact inputs |
| 2026-10-21 | A question about a finished task silently reopened it | One lifecycle table; reopening is a named operation |
| 2026-10-22 | A client update moved the preferred model; two days on the fallback unannounced | Missing route announced; a changed client runs the self-test; the self-test names its route |
| 2026-10-22 | Eleven corrections in an hour, each answered with a new fix; twelve diagnosis tasks asked for approval | One diagnosis per quiet thread, never a question; repeated corrections make the next reply a restatement |
| 2026-10-22 | A reply sat nine minutes while the messaging service refused it | Durable outbox; stalls visible |
| 2026-10-23 | A build accepted three times on reports crashed on the owner's first use | Verdicts say how the reviewer knows; checks run on the bytes; the builder's account travels with the verdict |
| 2026-10-23 | The evaluator graded prose against prose and passed a wrong file | Grading opens the deliverable; code decides objective items |
| 2026-10-24 | A worker that asked a question kept its slot; the next job timed out behind it | A pending question ends the turn and frees the slot |
| 2026-10-24 | A supervisor restart repeated every old failure notice | What the owner was told is kept on disk; routine results go to the dashboard |

Two patterns run through the list. Whenever the system guessed at something by reading text, it eventually guessed
wrong; each fix replaced the guess with a declared field or a record. And whenever it took an actor's word for its
own work, it eventually accepted a failure; each fix made the proof a record the runtime produces itself, and made
anything unproven say so to the owner. The name follows from the second pattern: the Tabularium was Rome's record
office, where the decisions and contracts that no magistrate's word could override were kept.

## Glossary

| Term | Meaning |
|---|---|
| Agent | One program with its own Telegram bot, folder, memory and skills |
| Owner | The one person the agents work for; the only judge |
| Turn | One message in, one reply out, with any tool calls between |
| Reply envelope | The structured reply: text plus declared claims the runtime checks |
| Task | A background job with a lifecycle, requirements, proof and a release record |
| Decision | A durable question or parked action, answered by the owner |
| Requirement | A numbered obligation of a task, optionally with a declared check |
| Proof | The recorded outcome of a check against exact bytes |
| Delegation | A contract from one agent to another, tracked to acceptance |
| Skill | A folder with a written method the agent loads when a task matches |
| Lesson | A candidate correction to a skill, trialled and approved by the owner |
| Supervisor | The process that starts, watches and restarts the others |
