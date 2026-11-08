"""One-off: the README, example, changelog, CI and notes follow the package layout."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def patch(rel, *pairs):
    path = ROOT / rel
    text = path.read_text(encoding="utf-8")
    for old, new in pairs:
        assert text.count(old) == 1, (rel, old[:70], text.count(old))
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")


patch("README.md",
      ("""```bat
git clone https://github.com/Tonguso/tabularium.git
cd tabularium
python -m venv .venv && .venv\\Scripts\\activate
pip install -r requirements.txt && pip install -e . --no-deps
copy config.example.bat config.bat
copy examples\\minimal\\SKILL.md cato\\SKILL.md && copy examples\\minimal\\memory.md cato\\memory.md
```

Then:

1. Create a bot with [@BotFather](https://t.me/BotFather) and get your own Telegram user id (message
   [@userinfobot](https://t.me/userinfobot)). Open `config.bat` and fill in `OWNER_TELEGRAM_USER_ID`,
   `TELEGRAM_BOT_TOKEN` and one model key, `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`. The agent answers your user id
   and refuses everyone else.
2. Open `cato\\SKILL.md` and write who the agent is and who you are. The example is a plain assistant; everything
   from the tone to what it must ask before doing is yours to change.
3. Start it:

```bat
call config.bat
python cato\\telegram.py
```

4. Message your bot. In a second terminal, `call config.bat && python dashboard.py` and open
   <http://127.0.0.1:8220> to see the agent's turns, tasks and anything waiting on you.

`cato` is the coordinator slot of the roster: the agent that can learn from your corrections and, once the other
agents exist, delegate to them. On its own it is a complete single agent. The example files are in
[examples/minimal](examples/minimal/README.md).
""",
       """```bat
git clone https://github.com/Tonguso/tabularium.git
cd tabularium
python -m venv .venv && .venv\\Scripts\\activate
pip install -r requirements.txt && pip install -e . --no-deps
mkdir %USERPROFILE%\\agents && cd %USERPROFILE%\\agents
tabularium init cato
```

`init` writes the agent's folder (`cato\\SKILL.md`, `memory.md`, `agent.toml`, `routing.json`) and the installation's
`.env` and `POLICY.md` into the current folder, the *home* of this installation. Then:

1. Create a bot with [@BotFather](https://t.me/BotFather) and get your own Telegram user id (message
   [@userinfobot](https://t.me/userinfobot)). Open `.env` and fill in `OWNER_TELEGRAM_USER_ID`,
   `TELEGRAM_BOT_TOKEN` and one model key, `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`. The agent answers your user id
   and refuses everyone else.
2. Open `cato\\SKILL.md` and write who the agent is and who you are. The template is a plain assistant; everything
   from the tone to what it must ask before doing is yours to change. `agent.toml` holds the settings that are the
   agent's own (its display name, its voice lines, its ports).
3. Start it:

```bat
tabularium run cato
```

4. Message your bot. In a second terminal, in the same folder, `tabularium dashboard` and open
   <http://127.0.0.1:8220> to see the agent's turns, tasks and anything waiting on you.

Every `tabularium` command works on the home folder: the current directory, or `--home PATH`, or `TABULARIUM_HOME`.
`cato` is the coordinator slot of the roster: the agent that can learn from your corrections and, once the other
agents exist, delegate to them. On its own it is a complete single agent. What the folder holds and how to shape the
agent is in [examples/minimal](examples/minimal/README.md).
"""),
      ("""The roster is one table in `agent_core/roster.py`. The repository ships four entry points:

| Slot | Folder | Role | Needs |
|---|---|---|---|
| Coordinator | `cato/` | Talks to you, researches, delegates, learns | `TELEGRAM_BOT_TOKEN` |
| Librarian | `callimachus/` | Evidence gathering and knowledge records for the coordinator; two parallel workers | `CALLIMACHUS_TELEGRAM_BOT_TOKEN` |
| Engineer | `vitruvius/` | Builds and tests artifacts on the machine for the coordinator | `VITRUVIUS_TELEGRAM_BOT_TOKEN` |
| Private agent | `tacitus/` | Personal matters in confidence: no shell, no delegation, no learning loop | `TACITUS_TELEGRAM_BOT_TOKEN` |

Each needs its own bot token, a `SKILL.md` and an empty `memory.md`. With more than one agent, run them under the
supervisor instead of by hand: `start_supervisor.bat` starts every configured agent and the dashboard, restarts a
crashed one, and runs the test gate before a release (`python agent_control.py restart`).
""",
       """The roster is one table in `src/tabularium/core/roster.py`; each slot names the role module that runs it:

| Slot | Folder in the home | Role module | Does | Needs |
|---|---|---|---|---|
| `cato` | `cato/` | `agents/coordinator.py` | Talks to you, researches, delegates, learns | `TELEGRAM_BOT_TOKEN` |
| `callimachus` | `callimachus/` | `agents/librarian.py` | Evidence gathering and knowledge records for the coordinator; two parallel workers | `CALLIMACHUS_TELEGRAM_BOT_TOKEN` |
| `vitruvius` | `vitruvius/` | `agents/engineer.py` | Builds and tests artifacts on the machine for the coordinator | `VITRUVIUS_TELEGRAM_BOT_TOKEN` |
| `tacitus` | `tacitus/` | `agents/private.py` | Personal matters in confidence: no shell, no delegation, no learning loop | `TACITUS_TELEGRAM_BOT_TOKEN` |

`tabularium init <slot>` writes each folder; each needs its own bot token. With more than one agent, run them under
the supervisor instead of by hand: `tabularium supervisor` starts every configured agent and the dashboard, restarts
a crashed one, and runs the test gate before a release (`tabularium restart`). On Windows, register it to start at
logon with Task Scheduler, task name `AgentsSupervisor`, action `pythonw -m tabularium supervisor` in the home
folder; `tabularium start` then starts or restarts everything, and `tabularium status` reports it.
"""),
      ("""Everything is an environment variable, set in `config.bat` (git-ignored) from `config.example.bat`. The minimal set
for one agent is three values; the rest are optional.
""",
       """Everything is an environment variable, set in the home folder's `.env` (git-ignored), which `tabularium init`
writes from the template. A variable already set in the environment wins over the file. The minimal set for one
agent is three values; the rest are optional.
"""),
      ("| `TAVILY_API_KEY`, `ANYSEARCH_API_KEY` | no | Web search providers, tried in the order in `search_order.json` |",
       "| `TAVILY_API_KEY`, `ANYSEARCH_API_KEY` | no | Web search providers, tried in the order set on the dashboard |"),
      ("""| Path | What it is |
|---|---|
| `engine.py`, `agent_core/` | The runtime: turn loop, reply envelope, tools, lifecycle, decisions, verification, memory, learning, outbox |
| `background_runtime.py` | Task store and background workers: queue, lanes, delegation register, acceptance, delivery |
| `supervisor.py`, `agent_control.py` | Process supervision, drain restarts, the test gate, status |
| `dashboard.py`, `dashboard/` | Local web page that reads the records; it never commands an agent |
| `<agent>/telegram.py` | One agent: its configuration, tools and persona on top of the shared `AgentApp` |
| `examples/minimal/` | A `SKILL.md` and `memory.md` to start the first agent from |
| `tests/`, `cato/tests/` | The offline test suites; a release runs both |
| `docs/` | The white paper, its figures, and the design notes |
""",
       """The code is one package, `src/tabularium/`; an installation's agents and records live in a separate home folder.

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

Outside the package: `cato/`, `callimachus/`, `vitruvius/`, `tacitus/` are the reference cast's folders (skills and
routing; no memory or records), so the repository root is itself a valid home for development. `tests/` is the
offline suite that a release runs, `docs/` the white paper, its figures and the design notes, `tools/` the scripts
that built this layout.

A home folder holds, per agent, `<slot>/SKILL.md`, `memory.md`, `agent.toml`, `skills/`, `routing.json` and the
agent's records under `task_state/`; and for the installation `.env`, `POLICY.md`, `logs/` and the dashboard's state
under `dashboard/`.
"""),
      ("""- Windows only. The batch files, the Task Scheduler supervisor, Outlook access and the desktop tool assume Windows;
  the engine, Telegram and SQLite do not. Cross-platform support is the next piece of work after packaging.
""",
       """- Windows first. The supervisor's process adoption, Outlook access and the desktop tool assume Windows; the engine,
  Telegram, SQLite and the command-line entry point do not. Cross-platform support is the next piece of work.
"""),
      ("Version 0.3. It runs daily, and it is not yet a drop-in product:", "Version 0.4 (in progress). It runs daily, and it is not yet a drop-in product:"),
      ("- The agents' prompts address the owner by a placeholder name. Making the owner's name a setting is open work.\n",
       "- The agents' prompts address the owner by a placeholder name. Making the owner's name a setting is open work.\n"
       "- The slots are the four of the roster. A fifth agent is a line in the roster and a role module; naming agents\n"
       "  freely in `agent.toml` is open work.\n"),
      ("| `CLAUDE_CODE_OAUTH_TOKEN` | no | Claude subscription route through the Claude Code CLI |",
       "| `CLAUDE_CODE_OAUTH_TOKEN` | no | Claude subscription route through the Claude Code CLI |\n"
       "| `TABULARIUM_HOME` | no | The home folder, when the command is not run inside it (`--home PATH` does the same) |"))

patch("examples/minimal/README.md",
      ("""# Minimal example: one agent

The smallest Tabularium deployment is one agent in the coordinator slot, talking to one person on Telegram. This
folder holds the two files that slot needs and the repository does not ship: the agent's identity and its memory.

| File | Copy to | What it is |
|---|---|---|
| `SKILL.md` | `cato/SKILL.md` | The agent's identity and standing instructions: the system prompt the runtime loads at start |
| `memory.md` | `cato/memory.md` | The memory file the agent reads on every turn and writes to with `remember` |

Steps, from the repository root with the dependencies installed (see the main [README](../../README.md)):

```bat
copy config.example.bat config.bat
copy examples\\minimal\\SKILL.md cato\\SKILL.md
copy examples\\minimal\\memory.md cato\\memory.md
```

Fill in `config.bat` (your Telegram user id, the bot token, one model key), edit `cato\\SKILL.md` so it describes
you and the agent you want, then:

```bat
call config.bat
python cato\\telegram.py
```

Message the bot. `/start` resets the conversation and reloads memory; `/memory` shows the memory file; `/tasks`
lists background work; the dashboard (`python dashboard.py`, then <http://127.0.0.1:8220>) shows turns, tasks and
anything waiting on your answer.

What the example deliberately leaves out: delegation (no librarian or engineer yet), the private agent, and any
skill beyond the identity file. Skills are folders under `cato/skills/` with a `SKILL.md` the agent loads when a task
matches; the repository ships the work-delegation skill as an example of the format.
""",
       """# Minimal example: one agent

The smallest Tabularium deployment is one agent in the coordinator slot, talking to one person on Telegram. An
agent is a folder of data in the installation's home; `tabularium init cato` writes it from the package's templates
([src/tabularium/templates](../../src/tabularium/templates)):

| File | What it is |
|---|---|
| `cato/SKILL.md` | The agent's identity and standing instructions: the system prompt the runtime loads at start |
| `cato/memory.md` | The memory file the agent reads on every turn and writes to with `remember` |
| `cato/agent.toml` | The agent's own settings: display name, voice lines, ports. Every key overrides a field of the role's default configuration; unknown keys are refused |
| `cato/routing.json` | The model routes, in order of preference; editable on the dashboard |
| `.env` | The installation's settings: your Telegram user id, the bot tokens, model keys |
| `POLICY.md` | The authority rules every agent carries in its prompt |

Steps, in an empty folder, with the package installed (see the main [README](../../README.md)):

```bat
tabularium init cato
```

Fill in `.env` (your Telegram user id, the bot token, one model key), edit `cato\\SKILL.md` so it describes you and
the agent you want, then:

```bat
tabularium run cato
```

Message the bot. `/start` resets the conversation and reloads memory; `/memory` shows the memory file; `/tasks`
lists background work; the dashboard (`tabularium dashboard`, then <http://127.0.0.1:8220>) shows turns, tasks and
anything waiting on your answer.

What the example deliberately leaves out: delegation (no librarian or engineer yet), the private agent, and any
skill beyond the identity file. Skills are folders under `cato/skills/` with a `SKILL.md` the agent loads when a task
matches; the repository ships the work-delegation skill as an example of the format.
"""))

patch("CHANGELOG.md",
      ("## [0.3.0] - 2026-11-08\n",
       """## [Unreleased]

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

## [0.3.0] - 2026-11-08
"""))

patch(".github/workflows/tests.yml",
      ("""      - name: Shared suite
        run: python -X utf8 -m pytest tests -q -p no:cacheprovider
      - name: Coordinator suite
        run: python -X utf8 -m pytest cato/tests -q -p no:cacheprovider
""",
       """      - name: Test suite
        run: python -X utf8 -m pytest tests -q -p no:cacheprovider
"""))

patch("CONTRIBUTING.md",
      ("code. The two suites (`tests/`, `cato/tests/`) must pass before a restart; the restart command refuses otherwise.",
       "code. The suite (`tests/`) must pass before a restart; the restart command refuses otherwise."))
patch("SECURITY.md",
      ("- **Credentials.** All keys live in `config.bat`, which is never committed,",
       "- **Credentials.** All keys live in the home folder's `.env`, which is never committed,"))
patch("callimachus/README.md",
      ("1. Set CALLIMACHUS_TELEGRAM_BOT_TOKEN in D:\\Agents\\config.bat to a NEW Telegram bot token.\n2. Double-click Start Cal.vbs in this folder. Do not run a duplicate copy of the bot.\n",
       "1. Set CALLIMACHUS_TELEGRAM_BOT_TOKEN in the home folder's .env to a NEW Telegram bot token.\n2. `tabularium run callimachus`, or let the supervisor start him. Do not run a duplicate copy of the bot.\n"),
      ("No existing agent was restarted. Cal is not yet part of the shared dashboard/start_all.\n", ""))
patch("vitruvius/README.md",
      ("enter its token in D:\\Agents\\config.bat under VITRUVIUS_TELEGRAM_BOT_TOKEN",
       "enter its token in the home folder's .env under VITRUVIUS_TELEGRAM_BOT_TOKEN"))
patch("docs/evaluation.md",
      ("Default cases live in agent_core/benchmark_cases.json,", "Default cases live in src/tabularium/learning/benchmark_cases.json,"),
      ("Run `python agent_control.py eval <agent> --k 1` for a first baseline,", "Run `tabularium eval <agent> --k 1` for a first baseline,"),
      ("agent_core/benchmark_cases.json), `repetitions`", "src/tabularium/learning/benchmark_cases.json), `repetitions`"),
      ("`python agent_control.py eval cato --interactions --k 2` uses the same regression", "`tabularium eval cato --interactions --k 2` uses the same regression"))
patch("docs/verification.md",
      ("`agent_core/verification.py` owns check execution", "`tabularium/records/verification.py` owns check execution"))
print("docs patched")
