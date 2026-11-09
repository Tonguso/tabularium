# Changelog

All notable changes to Tabularium are recorded here. The format follows Keep a Changelog; versions follow semantic
versioning.

## [Unreleased]

### Changed
- The code is one installable package, `tabularium`, grouped by what the modules do (core, records, channels,
  models, tools, memory, learning, delegation, ops, dashboard, agents). Every module keeps its name; only its place
  changed. Code and state are separate: an installation's agents, `.env`, `POLICY.md`, logs and dashboard state live
  in a home folder (`TABULARIUM_HOME`, `--home`, or the current directory).
- One command for every process: `tabularium init | run | dashboard | supervisor | status | restart | start | eval |
  lessons | exam | review`. Background workers start as `tabularium worker`. The batch launchers are gone.
- An agent is a folder of data. The four entry-point scripts became role modules under `tabularium.agents`
  (coordinator, librarian, engineer, private), the roster names which runs each slot, and an optional `agent.toml`
  in the agent's folder overrides any field of the role's configuration.
- Configuration is a `.env` file in the home folder, read by the command; API keys are read when an agent is built,
  not when a module is imported.
- One test suite (`tests/`); the coordinator's suite merged into it. The release gate runs it.
- After review of the layout (2026-11-09): release identity is the checkout that holds the package, not the home,
  so a home that is not a Git repository starts, and an installed package fingerprints its own files; the launch
  command names its home (`--home`) and the supervisor adopts only this installation's processes; the release gate
  runs the suites with the checkout as home and without the home's settings; a child process can be bound to another
  home (isolated evaluation runs); `agent.toml` values are validated, not coerced; the browser profile lives in the
  agent folder; pywin32 is needed only to store a ChatGPT connection; the scorecard's command line reads the home.
- For reproduction: `CITATION.cff`, `requirements.lock` (the frozen environment of the reported runs) and a README
  section on running the benchmark and what makes two runs comparable.

## [0.3.0] - 2026-11-08

### Added
- A README that starts one agent in ten minutes, an `examples/minimal` folder with the identity and memory files
  that agent needs, and a configuration file grouped into required, per-agent and optional settings.
- White paper 0.3 with four figures: the machine, one message start to finish, delegation by contract, the life of
  a lesson.
- The desktop as a tool for every agent: screen, mouse and keyboard, taken per task on the owner's yes, released by
  his next message, a timer, a credential window or his hand on the mouse.
- Every lesson carries its own benchmark case, calibrated over three runs per arm; a route change reruns the
  benchmark once.
- Standing matters in core memory: a message that names one arrives with its archived turns.
- The dashboard's "Waiting on you" page: parked actions, background tasks' questions and proposed skill edits,
  answered in place.

### Changed
- A verdict on a skill edit from the dashboard is settled by the runtime on arrival: drop retires, apply trials
  where the skill has cases and otherwise applies with the reason recorded.
- The owner's answers of the last two days ride in the live evidence of every turn and worker as rulings.
- Work that fits in a turn is done in the turn; background tasks are for research and long work.
- `send_photo` sends a local render with marks and a crop, so a question about a shape is a marked picture.
- An approved skill edit applies when the passage it changes is still present, not only when the file is unchanged.
- Lesson trials run on frozen candidates against reviewed development and reserved cases with artifact checks only.
- Delegation reworks are capped at three; completion is derived from the records and never prefixed onto the
  owner's message.

### Fixed
- Twenty-five approved skill edits that stood on the owner's list because applying depended on the agent.
- Foreground questions no longer appear as loose ends on the dashboard.
- The release gate no longer fails on a Windows file-handle race in a test's teardown.

## [0.1.0] - 2026-10-24

First public export of the framework behind a private four-agent operation.

### Added
- Turn loop with a declared reply envelope and runtime checks of its claims (work started, messages answered,
  corrections).
- Task store: one lifecycle table, durable decisions and parked actions, requirements with declared checks and
  byte-bound proof, derived completion, activations, release records.
- Delegation by contract: numbered completion items, parking without a slot, acceptance verdicts that say how the
  reviewer knows, the delegate's own account delivered unedited.
- Memory as one readable file with quoted provenance, a weekly exam, and reviewed consolidation.
- Owner-judged learning: candidate, proposed edit, trial, applied on the owner's words, verified or back.
- Durable outbox, announced model routes, supervisor with acknowledged restarts and truthful live status.
- Evaluation: a synthetic benchmark with deterministic checks, a research-quality signal, live probes, incident
  reviews; a dashboard that reads the records.
- Four agent entry points (coordinator, librarian, engineer, private agent) as working examples of the roster.
