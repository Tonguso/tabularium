# Tabularium

**An owner-judged agent framework: the records decide, not anyone's word.**

[![tests](https://github.com/Tonguso/tabularium/actions/workflows/tests.yml/badge.svg)](https://github.com/Tonguso/tabularium/actions/workflows/tests.yml)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)
[![White paper](https://img.shields.io/badge/docs-white%20paper-informational.svg)](docs/WHITEPAPER.md)

Tabularium runs a small cast of AI agents for one person over Telegram. Each agent is a process with its own bot,
memory, skills and tools; a supervisor keeps them up; a local dashboard shows what they are doing and what they are
waiting for. The framework's one rule: no part of the system believes what another part says about its own work. A
claim of work started, a question asked, a file delivered or a check passed is a record the runtime wrote and can
read back, or it did not happen.

It is the code behind a working system: four agents, five weeks of daily use, 676 offline tests. The design and the
failures that produced it are in the white paper: [docs/WHITEPAPER.md](docs/WHITEPAPER.md).

![One agent process and what surrounds it](docs/figures/fig1-machine.svg)

**Contents:** [Quickstart](#quickstart) · [What you get](#what-you-get) · [Add the rest of the cast](#add-the-rest-of-the-cast) ·
[Configuration](#configuration) · [How it works](#how-it-works) · [Layout](#layout) · [Status](#status) ·
[Contributing](#contributing) · [Citation](#citation) · [Licence](#licence)

## Quickstart

One agent, talking to you on Telegram, in about ten minutes. Windows, Python 3.12 or newer.

```bat
git clone https://github.com/Tonguso/tabularium.git
cd tabularium
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt && pip install -e . --no-deps
mkdir %USERPROFILE%\agents && cd %USERPROFILE%\agents
tabularium init coordinator
```

`init` writes the agent's folder (`coordinator\SKILL.md`, `memory.md`, `agent.toml`, `routing.json`) and the installation's
`.env` and `POLICY.md` into the current folder, the *home* of this installation. Then:

1. Create a bot with [@BotFather](https://t.me/BotFather) and get your own Telegram user id (message
   [@userinfobot](https://t.me/userinfobot)). Open `.env` and fill in `OWNER_TELEGRAM_USER_ID`,
   `TELEGRAM_BOT_TOKEN` and one model key, `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`. The agent answers your user id
   and refuses everyone else.
2. Open `coordinator\SKILL.md` and write who the agent is and who you are. The template is a plain assistant; everything
   from the tone to what it must ask before doing is yours to change. `agent.toml` holds the settings that are the
   agent's own (its display name, its voice lines, its ports, and `work_roots`: the folders outside its own that its
   file tools may read and write; without them an agent touches only its own folder).
3. Start it:

```bat
tabularium run coordinator
```

4. Message your bot. In a second terminal, in the same folder, `tabularium dashboard` and open
   <http://127.0.0.1:8220> to see the agent's turns, tasks and anything waiting on you.

Every `tabularium` command works on the home folder: the current directory, or `--home PATH`, or `TABULARIUM_HOME`.
`coordinator` is the coordinator slot of the roster: the agent that can learn from your corrections and, once the other
agents exist, delegate to them. On its own it is a complete single agent. What the folder holds and how to shape the
agent is in [examples/minimal](examples/minimal/README.md).

## What you get

- **One turn, one record.** Every message becomes a turn whose reply is a declared envelope, and its claims
  ("I started the job", "I answered your three messages") are checked against the task and inbox records before
  anything is sent. A promise with no job behind it goes back to the model.
- **Durable everything.** Questions to you, confirmations of risky actions, outgoing messages, task state, tool
  receipts and requirements live in SQLite and files. A crash, a restart or an answer that arrives hours later loses
  nothing.
- **Background work with proof.** Long jobs run in worker processes with checkpoints, numbered requirements and
  declared checks that run on the delivered bytes. "Finished" is derived from the records, never declared.
- **Delegation by contract.** A coordinator briefs another agent with numbered obligations; delivery is accepted
  from the checks, not the report; what no check could confirm reaches you named as unproven.
- **Owner-judged learning.** Agents turn your corrections into exact skill edits and change nothing until you say so.
  Your verdict from the dashboard settles at once; an approved rule becomes one tagged entry in the skill's ledger,
  counted helped or hurt on later tasks.
- **Memory that is tested.** One readable memory file per agent, writes that must quote you, a weekly exam, a
  weekly review that reads every sourced fact against what you said later, and tidy-ups applied only after the exam
  passes on both versions.
- **Operations like software.** A supervisor, self-tests, weekly scorecards, per-task traces, a regression
  benchmark, and releases only onto tested, committed code.

## Add the rest of the cast

The roster is one table in `src/tabularium/core/roster.py`; each slot names the role module that runs it:

| Slot | Folder in the home | Role module | Does | Needs |
|---|---|---|---|---|
| `coordinator` | `coordinator/` | `agents/coordinator.py` | Talks to you, researches, delegates, learns | `TELEGRAM_BOT_TOKEN` |
| `librarian` | `librarian/` | `agents/librarian.py` | Evidence gathering and knowledge records for the coordinator; two parallel workers | `LIBRARIAN_TELEGRAM_BOT_TOKEN` |
| `engineer` | `engineer/` | `agents/engineer.py` | Builds and tests artifacts on the machine for the coordinator | `ENGINEER_TELEGRAM_BOT_TOKEN` |
| `private` | `private/` | `agents/private.py` | Personal matters in confidence: no shell, no delegation, no learning loop | `PRIVATE_TELEGRAM_BOT_TOKEN` |

`tabularium init <slot>` writes each folder; each needs its own bot token. With more than one agent, run them under
the supervisor instead of by hand: `tabularium supervisor` starts every configured agent and the dashboard, restarts
a crashed one, and runs the test gate before a release (`tabularium restart`). On Windows, register it to start at
logon with Task Scheduler, task name `AgentsSupervisor`, action `pythonw -m tabularium supervisor` in the home
folder; `tabularium start` then starts or restarts everything, and `tabularium status` reports it.

## Configuration

Everything is an environment variable, set in the home folder's `.env` (git-ignored), which `tabularium init`
writes from the template. A variable already set in the environment wins over the file. The minimal set for one
agent is three values; the rest are optional.

| Variable | Required | What it is |
|---|---|---|
| `OWNER_TELEGRAM_USER_ID` | yes | Your Telegram user id; the only user the agents answer |
| `TELEGRAM_BOT_TOKEN` | yes | The coordinator's bot token; one token per agent, never shared |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | one of them | Model access. Both may be set; routing prefers the configured subscription route and falls back to the API |
| `CLAUDE_CODE_OAUTH_TOKEN` | no | Claude subscription route through the Claude Code CLI |
| `TABULARIUM_HOME` | no | The home folder, when the command is not run inside it (`--home PATH` does the same) |
| `LIBRARIAN_…`, `ENGINEER_…`, `PRIVATE_TELEGRAM_BOT_TOKEN` | per agent | Tokens for the other slots |
| `TAVILY_API_KEY`, `ANYSEARCH_API_KEY` | no | Web search providers, tried in the order set on the dashboard |
| `ELEVENLABS_API_KEY` | no | Spoken replies to voice notes |
| `EDGAR_IDENTITY` | no | Name and e-mail that SEC EDGAR requires of every client |
| `KNOWLEDGE_BASE_DIR` | no | The librarian's writable knowledge-base folder |

Subscription routes use the vendors' own clients and are the operator's responsibility under the vendors' terms.

## How it works

The white paper is the full account. The short version, with the figures from it:

- [One message, start to finish](docs/WHITEPAPER.md#33-one-message-start-to-finish): message, request built from
  the records, model and tools with receipts, a declared reply, claims checked, outbox.
- [Records](docs/WHITEPAPER.md#4-records): the task lifecycle table, decisions, requirements and proof, completion,
  receipts, the outbox.
- [Working together](docs/WHITEPAPER.md#5-working-together): delegation by contract, waiting without a slot,
  acceptance and the owner's view, privacy by role.
- [Learning](docs/WHITEPAPER.md#8-learning): an observation becomes a lesson when the agent names the decision in
  the method that allowed it; dockets and method reviews, the owner's verdict, trial, applied, measured.
- [Safety rails](docs/WHITEPAPER.md#9-safety-rails): what each one stops and how.

The larger mechanisms each have a design note with the failure that motivated it and the measurement before and
after: [verification](docs/verification.md), [requirements](docs/requirements.md), [completion](docs/completion.md),
[context loading](docs/context-loading.md), [evaluation](docs/evaluation.md),
[provider routing](docs/provider-routing.txt).

## Layout

The code is one package, `src/tabularium/`; an installation's agents and records live in a separate home folder.

| Path | What it is |
|---|---|
| `core/` | The runtime: engine (turn loop), `agent_app` (tools and handlers), reply envelope, roster, logging |
| `records/` | The task store (`background_runtime`), lifecycle table, decisions, requirements and proof, outbox, receipts |
| `channels/` | Telegram with the owner gate; the dashboard's message channel |
| `models/` | Provider routing, the Claude and ChatGPT subscription routes, usage accounting |
| `tools/` | Web search, page reading, SEC filings, the desktop, spreadsheets, video, the knowledge archive |
| `memory/` | The memory file, conversation recall, the weekly exam, consolidation, image memory |
| `learning/` | Lessons, trials, benchmark cases, corrections, the regression benchmark, interaction evaluation |
| `delegation/` | Briefs as contracts, the research and work clients, the audited local delegate command |
| `ops/` | Supervisor, release gate, retention, the operations snapshot, scorecards, the control commands |
| `dashboard/` | The local web server, its static page and the Android wrapper; it never commands an agent |
| `agents/` | `build()` and the four role modules: coordinator, librarian, engineer, private |
| `templates/` | What `tabularium init` writes: `SKILL.md`, `memory.md`, `agent.toml`, `routing.json`, `.env`, `POLICY.md` |
| `cli.py`, `paths.py`, `config.py` | The `tabularium` command, the home folder, the `.env` loader |

Outside the package: `coordinator/`, `librarian/`, `engineer/`, `private/` are the reference cast's folders (skills and
routing; no memory or records), so the repository root is itself a valid home for development. `tests/` is the
offline suite that a release runs, `docs/` the white paper, its figures and the design notes.

A home folder holds, per agent, `<slot>/SKILL.md`, `memory.md`, `agent.toml`, `skills/`, `routing.json` and the
agent's records under `task_state/`; and for the installation `.env`, `POLICY.md`, `logs/` and the dashboard's state
under `dashboard/`.

## Reproducing the evaluation

The benchmark is the versioned case set in `src/tabularium/learning/benchmark_cases.json` (each case carries its
version and the provenance of its expected values; see `docs/evaluation.md`). To run it:

```
tabularium eval coordinator --k 1          # one pass, a first baseline
tabularium eval coordinator --k 3          # three passes, for reliability
```

Results are written to `<home>/<agent>/evals/<timestamp>.json`, one file per run, with every requirement's verdict,
the artifact bytes checked (path and SHA-256), the release the agent ran, the model routes and the token accounting.
Two runs are comparable only when they share the case set, the oracle, the evaluator version and `--k`; the
comparison code refuses anything else.

`requirements.lock` is the exact environment of the runs reported in `docs/`, frozen with `pip freeze`
(`pyproject.toml` states minimums; the lock reproduces a run). Model outputs are not deterministic and the
subscription routes need a signed-in client, so a reproduction is a fresh run on the same cases and evaluator, not
a byte-for-byte replay. The measured evaluator comparison in `docs/evaluation.md` was made on the author's
installation; the cases and the evaluator it used are in this repository.

To cite the software, see `CITATION.cff`.

## Status

Version 0.5. It runs daily, and it is not yet a drop-in product:

- Windows first. The supervisor's process adoption and the desktop tool assume Windows; the engine,
  Telegram, SQLite and the command-line entry point do not. Cross-platform support is the next piece of work.
- The agents' prompts address the owner by a placeholder name. Making the owner's name a setting is open work.
- The slots are the four of the roster. A fifth agent is a line in the roster and a role module; naming agents
  freely in `agent.toml` is open work.
- Subscription model routes (Claude Code, the ChatGPT desktop app) depend on those clients being installed and
  signed in; the paid APIs are the fallback.
- The four entry points are the working examples of the roster; their skills, memory and the owner's data are not
  included.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md): a failing test first, records over guesses, a concept budget, one release a
day. Security concerns: [SECURITY.md](SECURITY.md).

## Citation

```bibtex
@misc{tabularium2026,
  title  = {Tabularium: An Owner-Judged Agent Framework},
  author = {Tong, Hogan},
  year   = {2026},
  url    = {https://github.com/Tonguso/tabularium}
}
```

## Licence

Apache License 2.0. See `LICENSE` and `NOTICE.md`.

## Disclaimer

Tabularium is a personal project extracted from one person's running system. It is not a product, carries no
warranty, and gives the agents real rights on the machine it runs on: a shell, the file system, email and a messaging
bot. Read [SECURITY.md](SECURITY.md) before running it.
