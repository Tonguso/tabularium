# Minimal example: one agent

The smallest Tabularium deployment is one agent in the coordinator slot, talking to one person on Telegram. An
agent is a folder of data in the installation's home; `tabularium init coordinator` writes it from the package's templates
([src/tabularium/templates](../../src/tabularium/templates)):

| File | What it is |
|---|---|
| `coordinator/SKILL.md` | The agent's identity and standing instructions: the system prompt the runtime loads at start |
| `coordinator/memory.md` | The memory file the agent reads on every turn and writes to with `remember` |
| `coordinator/agent.toml` | The agent's own settings: display name, voice lines, ports. Every key overrides a field of the role's default configuration; unknown keys are refused |
| `coordinator/routing.json` | The model routes, in order of preference; editable on the dashboard |
| `.env` | The installation's settings: your Telegram user id, the bot tokens, model keys |
| `POLICY.md` | The authority rules every agent carries in its prompt |

Steps, in an empty folder, with the package installed (see the main [README](../../README.md)):

```bat
tabularium init coordinator
```

Fill in `.env` (your Telegram user id, the bot token, one model key), edit `coordinator\SKILL.md` so it describes you and
the agent you want, then:

```bat
tabularium run coordinator
```

Message the bot. `/start` resets the conversation and reloads memory; `/memory` shows the memory file; `/tasks`
lists background work; the dashboard (`tabularium dashboard`, then <http://127.0.0.1:8220>) shows turns, tasks and
anything waiting on your answer.

What the example deliberately leaves out: delegation (no librarian or engineer yet), the private agent, and any
skill beyond the identity file. Skills are folders under `coordinator/skills/` with a `SKILL.md` the agent loads when a task
matches; the repository ships the work-delegation skill as an example of the format.
