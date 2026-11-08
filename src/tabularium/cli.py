"""The `tabularium` command: every process of an installation starts here.

    tabularium [--home PATH] init <slot>            write an agent folder (SKILL.md, memory.md, agent.toml) and .env
    tabularium [--home PATH] run <slot>             run one agent in the foreground
    tabularium [--home PATH] dashboard              the local web page
    tabularium [--home PATH] supervisor             the process that owns the agents and the dashboard
    tabularium [--home PATH] status | restart [slot ...] | start | open
    tabularium [--home PATH] eval <slot> [...] | lessons [slot ...] | exam | review
    tabularium [--home PATH] worker <task> --agent DIR --role SLOT [--attempt ID]     (started by the task store)

The home folder is --home, else TABULARIUM_HOME, else the current directory. Its .env is loaded before anything else
is imported, and the variable is set so that every child process (workers, agents the supervisor starts) uses the same
home. Slots are the roster's names: cato, callimachus, vitruvius, tacitus.
"""
import os
import shutil
import sys
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent / "templates"
LEGACY = {"status", "restart", "start", "open", "eval", "lessons", "exam", "review"}


def init(home, argv):
    """An agent folder from the templates, plus .env and POLICY.md if the home has none. Never overwrites."""
    from tabularium.core.roster import ROLES
    if len(argv) != 1 or argv[0] not in ROLES:
        return f"usage: tabularium init <slot>   (one of {', '.join(ROLES)})"
    slot = argv[0]
    folder = home / slot
    written = []
    for name, template in (("SKILL.md", "SKILL.md"), ("memory.md", "memory.md"), ("agent.toml", "agent.toml"),
                           ("routing.json", "routing.json")):
        target = folder / name
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(TEMPLATES / template, target)
            written.append(str(target))
    for name, template in ((".env", "env.example"), ("POLICY.md", "POLICY.md")):
        target = home / name
        if not target.exists():
            shutil.copyfile(TEMPLATES / template, target)
            written.append(str(target))
    (home / "logs").mkdir(exist_ok=True)
    lines = ["Written:"] + ["  " + w for w in written] if written else [f"Nothing to write: {folder} and the home files exist."]
    lines += ["", f"Next: fill in {home / '.env'} (your Telegram user id, the bot token, one model key), edit",
              f"{folder / 'SKILL.md'} so it describes you and the agent, then: tabularium run {slot}"]
    return "\n".join(lines)


def worker(argv):
    from tabularium.core.roster import ROLES
    from tabularium.records.background_runtime import worker as run_worker
    if len(argv) < 5 or "--agent" not in argv or "--role" not in argv:
        return "usage: tabularium worker <task id> --agent <agent dir> --role <slot> [--attempt <id>]"
    attempt = argv[argv.index("--attempt") + 1] if "--attempt" in argv else None
    run_worker(int(argv[0]), Path(argv[argv.index("--agent") + 1]), ROLES[argv[argv.index("--role") + 1]], attempt)
    return None


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--home"] and len(argv) > 1:
        os.environ["TABULARIUM_HOME"] = str(Path(argv[1]).resolve())
        argv = argv[2:]
    elif argv and argv[0].startswith("--home="):
        os.environ["TABULARIUM_HOME"] = str(Path(argv[0][7:]).resolve())
        argv = argv[1:]
    from tabularium import config, paths
    home = paths.home()
    os.environ["TABULARIUM_HOME"] = str(home)
    config.load_env(home)
    command = argv[0] if argv else "help"
    rest = argv[1:]
    if command == "init":
        print(init(home, rest))
    elif command == "run":
        from tabularium.core.roster import ROLES
        if len(rest) != 1 or rest[0] not in ROLES:
            sys.exit(f"usage: tabularium run <slot>   (one of {', '.join(ROLES)})")
        from tabularium.core.agent_logging import start_logging
        start_logging(rest[0])
        from tabularium.agents import build
        build(paths.agent_dir(rest[0]), rest[0]).main()
    elif command == "dashboard":
        from tabularium.dashboard import server
        server.main()
    elif command == "supervisor":
        from tabularium.ops import supervisor
        supervisor.main()
    elif command == "worker":
        message = worker(rest)
        if message:
            sys.exit(message)
    elif command in LEGACY:
        from tabularium.ops import agent_control
        agent_control.main(["start_all" if command == "start" else command] + rest)
    else:
        print(__doc__)
        if command not in ("help", "-h", "--help"):
            sys.exit(2)


if __name__ == "__main__":
    main()
