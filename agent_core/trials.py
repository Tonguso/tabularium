"""Trials: evidence before Alex approves a skill edit (2026-10-20).

A lesson used to go from 'proposed' to 'applied' on the strength of Cato's argument for it. A trial reruns one finished
past task twice in the background, once with the current skill (baseline) and once with the proposed edit (variant),
and sends Alex both answers with a short comparison. Alex stays the judge; the trial only gives him two real outputs
to judge instead of a promise. Nothing is adopted by the trial: approval still needs his own words (learning.approve).

How the variant sees the edit without anything changing on disk: the edited skill file is written to the trial folder,
and the worker maps the real skill path to it (AGENT_SKILL_OVERRIDES), so discover_skills, the system prompt and
read_file all read the edited text in that one process. Trial runs cannot send, deliver, delegate, start tasks or write
memory (tool scope), and write_file/edit_file only inside their own trial_output folder. A command run through
execute_command can still write elsewhere; the run is told not to, and the limit is stated here rather than hidden.
"""
import json
import os
import time
from pathlib import Path

from agent_core import learning
from agent_core.outcome import Refused

ELIGIBLE = ("basket_build", "basket_verification", "research")
BLOCKED_TOOLS = {"send_email", "send_document", "send_photo", "speak", "delegate", "background_task", "lesson",
                 "project_record", "remember", "log_activity"}
OVERRIDES_ENV = "AGENT_SKILL_OVERRIDES"
LOCAL = {}                       # in-process overrides (memory consolidation trials), never inherited by workers
TRIAL_DIR_ENV = "AGENT_TRIAL_DIR"


def _note(arm, output):
    return (f"\n\n[Trial run, {arm}: a rerun of a finished task to test a proposed skill edit. Nothing from this run goes "
            f"to Alex or anyone else: sending, delivering, delegating, starting tasks and writing memory are switched off. "
            f"Save every file you produce under {output} and nowhere else, including files a script writes; never "
            f"overwrite an existing deliverable. Otherwise do the task exactly as it was asked, as well as you can.]")


def start(agent_root, chat, key, source_task, store):
    """Start the two arms for lesson `key` on finished task `source_task`. Returns a summary for Cato."""
    agent_root = Path(agent_root).resolve()
    lesson = learning.read(agent_root, chat, key)
    if lesson.get("status") != "proposed" or not lesson.get("proposal"):
        raise ValueError("Only a proposed lesson can be trialled: propose the exact edit first.")
    if (lesson.get("trial") or {}).get("baseline") and not (lesson.get("trial") or {}).get("done"):
        raise ValueError("A trial for this lesson is already running.")
    task = store.get(source_task)
    if task["chat"] != chat or task["status"] != "finished":
        raise ValueError("Pick a finished task from this chat to rerun.")
    if learning.task_type_for(task["request"]) not in ELIGIBLE:
        raise ValueError("Trials rerun research and basket tasks only.")
    context = json.loads((store.directory(source_task) / "context.json").read_text(encoding="utf-8")).get("context", "")

    p = lesson["proposal"]
    method = Path(p["method_path"]).resolve()
    if learning._sha(method) != p["file_sha256"]:
        raise ValueError("The skill file changed since the proposal; propose the edit again first.")
    text = method.read_text(encoding="utf-8")
    edited = text.replace(p["current_text"], p["new_text"], 1) if p["current_text"] else text.rstrip("\n") + "\n\n" + p["new_text"] + "\n"
    folder = agent_root / "task_state" / "trials" / key
    patched = folder / method.relative_to(agent_root / "skills")
    patched.parent.mkdir(parents=True, exist_ok=True)
    patched.write_text(edited, encoding="utf-8")

    ids = {}
    for arm, overrides in (("baseline", {}), ("variant", {str(method): str(patched)})):
        label = {"baseline": "current skill", "variant": "with the proposed edit"}[arm]
        request = f"[Trial {key}, {label}] " + task["request"]
        row = store.start(chat, request, context, extra={"trial": {"lesson": key, "arm": arm, "source_task": int(source_task),
                                                                    "overrides": overrides}, "notify_telegram": False})
        ids[arm] = row["id"]
        out = store.directory(row["id"]) / "trial_output"
        out.mkdir(parents=True, exist_ok=True)
        ctx_path = store.directory(row["id"]) / "context.json"
        ctx = json.loads(ctx_path.read_text(encoding="utf-8"))
        ctx["context"] = ctx["context"] + _note(label, out)
        ctx_path.write_text(json.dumps(ctx, ensure_ascii=False, indent=2), encoding="utf-8")

    learning.set_trial(agent_root, chat, key, {"source_task": int(source_task), "baseline": ids["baseline"],
                                               "variant": ids["variant"], "started": time.time(), "done": False})
    return {"key": key, "source_task": int(source_task), "baseline_task": ids["baseline"], "variant_task": ids["variant"],
            "note": "Both runs are queued and send nothing. When the second finishes, Alex gets both answers with a comparison."}


def isolated(context):
    """True for a run that must not reach anyone: a lesson trial or a regression-eval rerun (agent_core.regression)."""
    context = context or {}
    return bool(context.get("trial") or context.get("eval"))


def prepare_worker(context, folder):
    """Before the agent module loads in an isolated worker: fence file writes; for a trial, point skill reads at the
    edited file."""
    if not isolated(context):
        return
    os.environ[TRIAL_DIR_ENV] = str(Path(folder) / "trial_output")
    if (context.get("eval") or {}).get("benchmark"):
        (Path(folder) / "trial_output").mkdir(parents=True, exist_ok=True)
    trial = (context or {}).get("trial")
    if trial:
        os.environ[OVERRIDES_ENV] = json.dumps(trial.get("overrides") or {})


def scope(runtime, context=None):
    """In an isolated worker after the agent loads: only non-outward tools exist."""
    names = {(t.get("function") or t).get("name") for t in runtime._all_tools("anthropic")}
    runtime._tool_scope = names - BLOCKED_TOOLS
    if ((context or {}).get("eval") or {}).get("benchmark"):
        runtime._tool_scope &= {"read_file", "write_file", "task_checkpoint", "verify_requirement"}


def overridden(path):
    """The edited copy of a skill or memory file for this process, or None."""
    mapping = json.loads(os.environ.get(OVERRIDES_ENV) or "{}")
    if not mapping and not LOCAL:
        return None
    try:
        key = str(Path(path).resolve())
    except OSError:
        return None
    return LOCAL.get(key) or mapping.get(key)


class local_override:
    """`with local_override(path, replacement):` reads of `path` in this process see `replacement`."""
    def __init__(self, path, replacement):
        self.key, self.replacement = str(Path(path).resolve()), str(replacement)

    def __enter__(self):
        LOCAL[self.key] = self.replacement

    def __exit__(self, *exc):
        LOCAL.pop(self.key, None)


def write_refusal(path):
    """In a trial worker, writes outside its own trial_output folder are refused."""
    fence = os.environ.get(TRIAL_DIR_ENV)
    if not fence:
        return None
    try:
        inside = Path(path).resolve().is_relative_to(Path(fence).resolve())
    except (OSError, ValueError):
        inside = False
    return None if inside else Refused(f"Trial run: files go under {fence} only; nothing was written to {path}.")


COMPARE = ("Two runs of the same past task, for Alex to judge a proposed change to one of your skills. Run A used the "
           "current skill. Run B used the skill with this edit:\n\n{edit}\n\nOriginal request:\n{request}\n\n=== Run A "
           "(current skill) ===\n{a}\n\n=== Run B (with the edit) ===\n{b}\n\nWrite, in Alex's language (简体中文 unless "
           "the request is in English), exactly three short numbered lines: 1. what actually differs between the two "
           "answers, with specifics; 2. which one meets the original request better, and why; 3. any risk or cost the "
           "edit introduces. Then one line starting 建议: approve, reject, or rerun on another task. Do not invent "
           "differences; if they are the same in substance, say so.")


def after_arm(agent, store, ident, context, result):
    """Called by a trial worker with its final answer. The first arm to finish stays silent. The second writes the
    comparison and returns the message Alex receives (its own answer is kept as answer.txt)."""
    trial = (context or {}).get("trial")
    if not trial:
        return result
    folder = store.directory(ident)
    (folder / "answer.txt").write_text(result or "", encoding="utf-8")
    root = store.agent_dir.resolve()
    lesson = learning.read(root, store.get(ident)["chat"], trial["lesson"])
    record = lesson.get("trial") or {}
    other = record.get("variant") if trial["arm"] == "baseline" else record.get("baseline")
    if not other or not (store.directory(other) / "answer.txt").exists():
        return result                                               # the other run has no answer yet: stay silent
    answers = {trial["arm"]: result or ""}
    other_answer = store.directory(other) / "answer.txt"
    answers["variant" if trial["arm"] == "baseline" else "baseline"] = other_answer.read_text(encoding="utf-8") if other_answer.exists() else ""
    p = lesson["proposal"]
    edit = (f"REPLACE:\n{p['current_text']}\nWITH:\n{p['new_text']}" if p["current_text"] else f"ADD:\n{p['new_text']}")
    source = store.get(record["source_task"])
    prompt = COMPARE.format(edit=edit, request=source["request"][:4000], a=answers["baseline"][:12000], b=answers["variant"][:12000])
    messages = agent.runtime.load_memory_context() + [{"role": "user", "content": f"{agent.runtime._ts()} {prompt}"}]
    with agent.runtime.tool_scope(set()):
        comparison = str(agent.runtime.run_turn(messages, store.get(ident)["chat"]) or "").strip()
    report = (f"【技能修改试验】{lesson.get('title', trial['lesson'])}\n"
              f"重跑了任务 #{record['source_task']}，一次用现有技能（#{record['baseline']}），一次用提议的修改（#{record['variant']}）。\n\n"
              f"{comparison}\n\n两个完整回答：\n- 现有技能：{store.directory(record['baseline']) / 'answer.txt'}\n"
              f"- 修改后：{store.directory(record['variant']) / 'answer.txt'}\n"
              f"批准或不批准，直接跟我说；这次试验没有改动任何东西。")
    (root / "task_state" / "trials" / trial["lesson"] / "report.md").write_text(report, encoding="utf-8")
    learning.set_trial(root, store.get(ident)["chat"], trial["lesson"], dict(record, done=True, finished=time.time(),
                                                                             report=str(root / "task_state" / "trials" / trial["lesson"] / "report.md")))
    ctx_path = folder / "context.json"
    ctx = json.loads(ctx_path.read_text(encoding="utf-8"))
    ctx["notify_telegram"] = True                                   # this arm's notice carries the report to Alex
    ctx_path.write_text(json.dumps(ctx, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
