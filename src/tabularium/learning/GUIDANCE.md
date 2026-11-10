## Lessons: a change of method, not a reminder

A failure you notice is an observation. A lesson is a claim about the decision in a method that allowed it: the rule,
default or omission without which the failure could not have happened. Before proposing, read the method file and the
open observations (project_record list kind=lesson) and name the ones the cause explains. The edit removes the cause;
a rule that must be remembered means the method still permits the failure.

One flow for every lesson: record, show, apply on Alex's go, measure.
1. Propose: remember(kind='lesson', content, source, method_path, cause, anchor or silent_at, explains, old, new), or
   lesson(action='propose') on an observation already recorded. The anchor is quoted exactly from the method and the
   edit rewrites it; old is empty only where the method is silent, and the ledger never grows past five entries. The
   runtime shows the edit to Alex on the dashboard and in the weekly message; send it yourself only if he asks.
2. His verdict arrives as "Skill edit <key>: apply it", "apply it, no trial needed", "drop it" or "draft a case
   first", and the runtime settles it before your turn. "apply it" runs the trial on the skill's cases; without usable
   cases nothing is applied and he chooses. When he asks for a case first, fill and calibrate the lesson's case
   (lesson action=case with its fixtures, then calibrate).
3. Measure: on the next real task where the failure could recur, lesson(action='checked', key, worked, evidence,
   task, receipt, method_sha). A check without those three is a judgement and counts nothing.
4. Themes: the runtime proposes membership for observations that look alike and opens a method review at three in
   sixty days. Confirm or reverse membership (lesson action=group / ungroup) and answer the review with one cause and
   one edit for the theme. Fold (lesson action=fold) absorbs ledger entries with measured checks into the prose.
Skills are yours to change this way; runtime code is not: for a lesson that needs a code change, write the runtime
report and leave the edit to the coding agents.
