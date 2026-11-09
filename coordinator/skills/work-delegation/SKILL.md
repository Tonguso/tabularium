---
name: work-delegation
description: How the coordinator hands work to the librarian or the engineer and accepts it. Load before using the delegate tool. A generic starting point; replace it with your own practice.
metadata:
  author: Tabularium
  version: "0.1.0"
---

# Work delegation (example)

Delegate when another agent holds a capability you lack, when work can run in parallel with yours, or when an
independent check would improve the result. Keep the judgment yourself.

## Writing the brief

- State the decision or deliverable the work serves, and what is already known, with paths, not pasted history.
- Give numbered completion items, critical ones first, each with how you will check it. Item 1 is a small early
  slice the delegate finishes first, so you can correct course before the rest.
- Declare a check for each item you can check mechanically: a command to run against the delivered file, or text it
  must contain. The runtime runs it on the delivered bytes.
- Set `budget_tokens` as a target the delegate sizes its work by. It is never a stop.
- Label the owner's decisions as decisions, with the owner's words, and your own assumptions as assumptions.

## While it runs

- Read the delegate's checkpoint at the early slice. Rework or rescope then, not after a result you cannot use.
- Answer a question the delegate stops on with `delegate(action='answer')`. If it is the owner's decision, ask the
  owner and pass the answer on as theirs.
- Do not poll in a loop. Finish independent work, checkpoint, and end the turn; the runtime parks your task without a
  worker slot and wakes it when the result returns.

## Accepting the result

- Read the DONE / OPEN / EVIDENCE / COST lines first. They reach the owner unedited under your verdict.
- Give one verdict per item, saying how you know: ran, read, or report. Report-only items stay unverified and are
  named to the owner as not run by you.
- Rework critical items that are not met, or state the gap plainly in your deliverable. Never call a deliverable
  ready on a report alone.
