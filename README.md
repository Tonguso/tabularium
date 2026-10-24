# Tabularium

**An owner-judged agent framework: the records decide, not anyone's word.**

[![tests](https://github.com/Tonguso/tabularium/actions/workflows/tests.yml/badge.svg)](https://github.com/Tonguso/tabularium/actions/workflows/tests.yml)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)
[![White paper](https://img.shields.io/badge/docs-white%20paper-informational.svg)](docs/WHITEPAPER.md)

Tabularium runs a small cast of AI agents for one person over Telegram, on one Windows PC. It is the code behind a
working system (four agents, three weeks of daily use, about 500 tests), published so that others can build the same
thing or take the parts they need. The design is described in the white paper: [docs/WHITEPAPER.md](docs/WHITEPAPER.md).

**Contents:** [What it does](#what-it-does) · [Install](#install) · [Quickstart](#quickstart) · [Status](#status) ·
[Layout](#layout) · [Design notes](#design-notes) · [Contributing](#contributing) · [Citation](#citation) ·
[Licence](#licence) · [Disclaimer](#disclaimer)

The name is the Roman public record office. Every rule in the framework is the same rule in a different place: where a
system could believe what the model, a worker, a delegate or a grader says about its own work, Tabularium checks a
stored record instead, and anything unproven says so to the owner.

## What it does

- **One turn, one record.** Every message becomes a turn whose reply is a declared field (not whatever the model
  typed last), and claims in it ("I started the job", "I answered your three messages") are checked against the task
  and inbox records before anything is sent.
- **Durable everything.** Questions to the owner, confirmations of risky actions, outgoing messages, task state,
  tool receipts and requirements live in SQLite and files, so a crash, a restart or a late answer loses nothing.
- **Delegation by contract.** A coordinator writes numbered obligations for another agent; delivery is blocked until
  the result is read and accepted; verdicts say how the reviewer knows; declared checks run on the delivered bytes;
  the delegate's own account travels to the owner unedited.
- **Owner-judged learning.** Agents turn mistakes into candidate lessons and exact skill edits, trial them against
  past tasks, and change nothing until the owner says so in his own words.
- **Memory that is tested.** One readable memory file per agent, writes that must quote the owner, a weekly exam, and
  tidy-ups applied only after the exam passes on both versions.
- **Operations like software.** A supervisor, daily self-tests, a self-test after every model-client update, weekly
  scorecards, per-task traces, a regression benchmark, a research-quality signal, incident reviews, and restarts only
  onto tested, committed code.

## Status

Version 0.1, extracted from a private operation on 2026-10-24. It runs, but it is not yet a drop-in product:

- Windows only; paths to a miniconda environment and `D:\Agents` are hard-coded in the batch files.
- The agents' prompts address the owner by a placeholder name (Alex). Making the owner's name a setting is open work.
- Model access uses the vendors' own clients for subscription routes (Claude Code, the ChatGPT desktop app) and the
  paid APIs as fallbacks; subscription routes are the operator's responsibility under the vendors' terms.
- The four agent entry points (`cato`, `callimachus`, `vitruvius`, `tacitus`) are included as working examples of
  the roster; their skills, memory and the owner's data are not.

## Layout

| Path | What it is |
|---|---|
| `engine.py`, `agent_core/` | The runtime: turn loop, reply envelope, tools, lifecycle, decisions, verification, memory, learning, outbox |
| `background_runtime.py` | Task store and background workers: queue, lanes, delegation register, acceptance, delivery |
| `supervisor.py`, `agent_control.py` | Process supervision, drain restarts, self-tests, status |
| `dashboard.py`, `dashboard/` | Local web page that reads the records; it never commands the agents |
| `<agent>/telegram.py` | One agent: its configuration, tools and persona on top of the shared `AgentApp` |
| `tests/`, `cato/tests/` | The offline test suites; a restart runs both |
| `docs/` | The white paper and the design notes behind the larger changes |

## Install

Windows, Python 3.12 or newer.

```bat
git clone https://github.com/Tonguso/tabularium.git
cd tabularium
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
pip install -e . --no-deps
python -m pytest
```

The editable install puts the framework's modules on the interpreter's path, which is how each agent's scripts find
the shared code when started as their own process. The last line runs the two offline suites; they make no model
calls. Optional extras: `faster-whisper` for voice
notes, `playwright` for browser tools, and the Claude Code CLI if you use the Claude subscription route.

## Quickstart

1. Copy `config.example.bat` to `config.bat` and replace every `<placeholder>`: your Telegram user id, one bot
   token per agent, and the model keys you use. `config.bat` is git-ignored; never commit it. The agents answer only
   the user id in `OWNER_TELEGRAM_USER_ID`.
2. Give each agent you intend to run a `SKILL.md` (its identity) and an empty `memory.md` in its folder.
3. Run `start_supervisor.bat`. The supervisor starts the agents and the dashboard at `http://127.0.0.1:8220`.
4. Message your bot. Operations from the shell:

```bat
python agent_control.py status
python agent_control.py restart
python agent_control.py eval cato --k 3
python agent_control.py lessons
```

## Design notes

The white paper is the overview. The larger mechanisms each have a design note with the failure that motivated it,
the measurement before and after, and the limits: [verification](docs/verification.md),
[requirements](docs/requirements.md), [completion](docs/completion.md), [context loading](docs/context-loading.md),
[evaluation](docs/evaluation.md), [provider routing](docs/provider-routing.txt).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md): a failing test first, records over guesses, a concept budget, one release a
day. Security concerns: [SECURITY.md](SECURITY.md). Changes: [CHANGELOG.md](CHANGELOG.md).

## Citation

If you build on Tabularium, cite the white paper:

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
