# Minimal example: one agent

The smallest Tabularium deployment is one agent in the coordinator slot, talking to one person on Telegram. This
folder holds the two files that slot needs and the repository does not ship: the agent's identity and its memory.

| File | Copy to | What it is |
|---|---|---|
| `SKILL.md` | `cato/SKILL.md` | The agent's identity and standing instructions: the system prompt the runtime loads at start |
| `memory.md` | `cato/memory.md` | The memory file the agent reads on every turn and writes to with `remember` |

Steps, from the repository root with the dependencies installed (see the main [README](../../README.md)):

```bat
copy config.example.bat config.bat
copy examples\minimal\SKILL.md cato\SKILL.md
copy examples\minimal\memory.md cato\memory.md
```

Fill in `config.bat` (your Telegram user id, the bot token, one model key), edit `cato\SKILL.md` so it describes
you and the agent you want, then:

```bat
call config.bat
python cato\telegram.py
```

Message the bot. `/start` resets the conversation and reloads memory; `/memory` shows the memory file; `/tasks`
lists background work; the dashboard (`python dashboard.py`, then <http://127.0.0.1:8220>) shows turns, tasks and
anything waiting on your answer.

What the example deliberately leaves out: delegation (no librarian or engineer yet), the private agent, and any
skill beyond the identity file. Skills are folders under `cato/skills/` with a `SKILL.md` the agent loads when a task
matches; the repository ships the work-delegation skill as an example of the format.
