# Contributing

Tabularium was built by one owner with two coding agents working on the same code. The rules that made that work are
the rules for contributions here.

- **Fix the design, not the symptom.** A bug is a decision that allowed it. Name the decision, find every path that
  shares it, prefer deleting a path to guarding it, and prove the class is gone with a test that failed before.
- **A failing test first.** Every change to the runtime comes with a test that reproduces the failure on the old
  code. The suite (`tests/`) must pass before a restart; the restart command refuses otherwise.
- **Records over guesses.** Do not add a rule in a prompt where a record and a check in code would do. Do not add a
  check that trusts an actor's account of its own work.
- **Concept budget.** A change that adds a new named thing (a table, a state, a file kind, a term that can reach the
  owner's chat) removes one, or says why it cannot.
- **One release a day.** Commit as you go; the restart that takes changes live happens once a day in a quiet hour.
- **Write it down.** Every change gets an entry in the operations log naming what changed, why, what was measured,
  and what the other author did alongside. A failure the owner noticed gets an incident review.
- **Never read the private agent.** The private agent's memory and conversations are off limits to maintainers;
  only tool records may be inspected.
- **No author reverts another's change** until the owner confirms. Disagreements go to the owner with both views.
