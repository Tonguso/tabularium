"""Paired lesson screening using the shared independent artifact evaluator.
Freeze cases and skills before scheduling. Only settled runs are graded, including failures.
No model-written recommendation and no automatic adoption.
"""
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from tabularium.learning import learning
from tabularium.learning import regression
from tabularium.core.outcome import Refused

OVERRIDES_ENV = "AGENT_SKILL_OVERRIDES"
TRIAL_DIR_ENV = "AGENT_TRIAL_DIR"
READS_ENV = "AGENT_TRIAL_READS"
LOCAL = {}
BLOCKED_TOOLS = {"send_email", "send_document", "send_photo", "speak", "delegate", "background_task", "lesson", "project_record", "remember", "log_activity", "desktop"}


def proposal_id(proposal):
    return hashlib.sha256(json.dumps(proposal, sort_keys=True).encode()).hexdigest()


def evidence_matches(lesson):
    r = lesson.get("trial") or {}
    if not (r.get("done") and r.get("notified") and r.get("version") == 2 and not r.get("scheduling_error")
            and r.get("proposal") == proposal_id(lesson.get("proposal") or {})):
        return False
    expected = len(r.get("cases", [])) * r.get("repetitions", 0) * 2
    if not expected or len(r.get("outcomes", [])) != expected:
        return False
    try:
        return learning._sha(Path(r["folder"]) / "variant.md") == r["candidate_sha"]
    except OSError:
        return False



def case_identity(case):
    return regression.fingerprint([{k: case.get(k) for k in ("request", "context")}])


def start(agent_root, chat, key, spec, store, system_prompt="Complete the supplied task using the supplied skill."):
    root = Path(agent_root).resolve()
    lesson = learning.read(root, chat, key)
    if lesson.get("status") != "proposed" or not lesson.get("proposal"):
        raise ValueError("Only a proposed lesson can be trialled.")
    previous = lesson.get("trial") or {}
    if previous.get("version") == 2 and not previous.get("notified"):
        raise ValueError("A trial is already running; finish its workers first.")
    if previous and previous.get('version') != 2:
        old_ids = [previous[n] for n in ('baseline', 'variant') if previous.get(n)]
        if any(store.get(i)['status'] in regression.ACTIVE or store.lease_busy(i) for i in old_ids):
            raise ValueError('Legacy trial workers still active; stop them before scheduling reviewed cases.')
    if not isinstance(spec, dict):
        raise ValueError("Select reviewed development_cases and heldout_cases; past-task replay is retired.")
    from tabularium.learning import lesson_cases
    spec = lesson_cases.default_spec(root, chat, key, spec)      # the skill's own approved cases when none are chosen (2026-10-26)
    development, heldout = spec.get("development_cases", []), spec.get("heldout_cases", [])
    ids = development + heldout
    if not development or not heldout or len(set(ids)) != len(ids):
        raise ValueError("Development and held-out cases must be nonempty, distinct and disjoint.")
    repetitions, steps = spec.get("repetitions", 2), spec.get("max_steps", 12)
    if type(repetitions) is not int or not 1 <= repetitions <= 5 or type(steps) is not int or not 1 <= steps <= 60:
        raise ValueError("Use 1–5 repetitions and 1–60 steps per run.")
    available = {c["id"]: c for c in regression.load_cases(root)}
    if any(i not in available for i in ids):
        raise ValueError("Use reviewed benchmark IDs; collected answers are not independent oracles.")
    cases = [available[i] for i in ids]
    history = lesson.get("trial_history", []) + ([previous] if previous else [])
    exposed = {f for r in history for f in r.get("exposed", [])}
    if any(case_identity(available[i]) in exposed for i in heldout):
        raise ValueError("Held-out inputs already exposed by this lesson; use fresh cases.")
    p = lesson["proposal"]
    method = Path(p["method_path"]).resolve()
    learning.still_applies(method, p)
    baseline = method.read_text(encoding="utf-8")
    folder = root / "task_state" / "trials" / key / uuid.uuid4().hex
    folder.mkdir(parents=True)
    for arm, text in (("baseline", baseline), ("variant", learning.render_change(baseline, key, p))):
        (folder / (arm + ".md")).write_text(text, encoding="utf-8")
    record = dict(version=2, proposal=proposal_id(p), folder=str(folder), cases=cases,
                  splits={i: "development" if i in development else "heldout" for i in ids},
                  exposed=[case_identity(c) for c in cases], repetitions=repetitions, max_steps=steps,
                  started=time.time(), done=False, ready=False, runs=[], system_prompt=system_prompt,
                  baseline_sha=learning._sha(folder / "baseline.md"), candidate_sha=learning._sha(folder / "variant.md"))
    def reserve(d):
        if d.get('status') != 'proposed' or proposal_id(d['proposal']) != record['proposal']:
            raise ValueError('Proposal changed while preparing trial.')
        if d.get('trial', {}).get('version') == 2 and not d['trial'].get('notified'):
            raise ValueError('A trial is already running.')
        if d.get('trial'):
            d.setdefault('trial_history', []).append(d['trial'])
        d['trial'] = record
    learning._update(root, chat, key, reserve)
    try:
        for case in cases:
            for attempt in range(repetitions):
                for arm in (("baseline", "variant") if attempt % 2 == 0 else ("variant", "baseline")):
                    skill = folder / (arm + ".md")
                    trial = dict(lesson=key, cohort=str(folder), arm=arm, max_steps=steps,
                                 skill=str(skill), skill_sha=learning._sha(skill),
                                 system_prompt=skill_prompt(system_prompt, baseline, skill.read_text(encoding="utf-8")),
                                 overrides={str(method): str(skill)}, fixtures=case.get("fixtures") or {})
                    row = store.start(chat, f"[Evaluation {folder.name}/{len(record['runs'])}] " + case["request"],
                                      str(case.get("context", "")), extra={"trial": trial, "notify_telegram": False})
                    record["runs"].append(dict(task=row["id"], case=case["id"], attempt=attempt + 1, arm=arm))
                    learning.set_trial(root, chat, key, record)
    except Exception as exc:
        record["scheduling_error"] = str(exc)
        raise
    finally:
        record["ready"] = True
        learning.set_trial(root, chat, key, record)
    return dict(key=key, tasks=[r["task"] for r in record["runs"]], note="Frozen screening queued; file checks and usage follow after all runs settle. Approval still controls adoption.")


def isolated(context):
    return bool((context or {}).get("trial") or (context or {}).get("eval"))


def prepare_worker(context, folder):
    if not isolated(context):
        return
    output = Path(folder) / "trial_output"
    output.mkdir(parents=True, exist_ok=True)
    os.environ[TRIAL_DIR_ENV] = str(output)
    trial = context.get("trial")
    if trial:
        if not trial.get("cohort"):
            raise ValueError("Legacy trial retired; schedule reviewed cases for the current proposal.")
        if learning._sha(trial["skill"]) != trial["skill_sha"]:
            raise ValueError("Frozen trial skill changed.")
        os.environ[OVERRIDES_ENV] = json.dumps(trial["overrides"])
        os.environ[READS_ENV] = json.dumps([trial["skill"]])
        for f in (trial.get("fixtures") or {}).get("files", []):      # declared inputs, inside the run's own folder
            target = output / "inputs" / f["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f.get("content", ""), encoding="utf-8")


def scope(runtime, context=None):
    names = {(t.get("function") or t).get("name") for t in runtime._all_tools("anthropic")}
    runtime._tool_scope = names - BLOCKED_TOOLS
    trial = (context or {}).get("trial")
    if trial:
        fixtures = trial.get("fixtures") or {}
        allowed = {"read_file", "write_file"} | {SCRIPTED[k] for k in fixtures if k in SCRIPTED}
        runtime._tool_scope = names & allowed          # a scripted tool answers from the fixture, never live (scripted())
        runtime._trial = trial
        runtime.MAX_TOOL_ITERATIONS = trial["max_steps"]
    elif ((context or {}).get("eval") or {}).get("benchmark"):
        runtime._tool_scope &= {"read_file", "write_file", "task_checkpoint", "verify_requirement"}


def skill_prompt(prompt, baseline, candidate):
    # Standing methods may already be in the seed. Replace that copy instead of injecting contradictory rules.
    body = lambda text: text.partition('\n---')[2].lstrip() if text.startswith('---') and '\n---' in text else text
    before, after = body(baseline), body(candidate)
    if before and before in prompt:
        return prompt.replace(before, after)
    return prompt + "\n\nSkill under evaluation:\n" + candidate


SCRIPTED = {"delegate": "delegate", "verify": "execute_command", "search": "web_search"}   # fixture -> the tool it answers


def worker_prompt(context):
    prompt = context['trial']['system_prompt']
    files = (context['trial'].get('fixtures') or {}).get('files') or []
    if files:
        base = Path(os.environ.get(TRIAL_DIR_ENV, '')) / 'inputs'
        prompt += '\n\nInput files for this task (read them with read_file): ' + ', '.join(str(base / f['path']) for f in files)
    return prompt


def scripted(runtime, name, inputs):
    """In a trial worker the declared fixtures answer the delegate, the verification command and the search with
    canned results, so a method about them can be exercised without a side effect (2026-11-10). None outside a
    trial, and for a tool the case declared no fixture for (the scope has already refused it)."""
    trial = getattr(runtime, '_trial', None)
    fixtures = (trial or {}).get('fixtures') or {}
    if not trial or not fixtures:
        return None
    if name == 'delegate' and 'delegate' in fixtures:
        d, action = fixtures['delegate'], inputs.get('action')
        if action == 'submit':
            return json.dumps({'request_id': 'scripted-1', 'status': 'submitted',
                               'note': 'Scripted delegate (trial fixture): the result is ready; read it with action=result.'})
        if action in ('result', 'status'):
            return json.dumps({'request_id': inputs.get('request_id', 'scripted-1'), 'status': d.get('status', 'completed'), 'text': d['result']})
        return json.dumps({'request_id': inputs.get('request_id', 'scripted-1'), 'status': 'recorded', 'note': f'Scripted delegate: {action} recorded.'})
    if name == 'execute_command' and 'verify' in fixtures:
        v = fixtures['verify']
        return f"[Scripted verification (trial fixture)] exit code {int(v.get('exit_code', 0))}\n{v.get('output', '')}"
    if name == 'web_search' and 'search' in fixtures:
        return '\n\n'.join(f"{r.get('title', '')}\n{r.get('url', '')}\n{r.get('snippet', '')}" for r in fixtures['search']) or 'No results.'
    return None



def read_refusal(path):
    reads = os.environ.get(READS_ENV)
    if reads is None:
        return None
    resolved = Path(path).resolve()
    if resolved in {Path(p).resolve() for p in json.loads(reads)} or resolved.is_relative_to(Path(os.environ[TRIAL_DIR_ENV]).resolve()):
        return None
    return Refused("Trial reads are limited to the frozen skill and this run's outputs. Task inputs are in the request.")

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



def reconcile(store, send):
    db = learning.ProjectStore(store.agent_dir).connect()
    try:
        rows = list(db.execute("SELECT chat,key,data FROM records WHERE kind='lesson' AND json_extract(data, '$.trial.version')=2"))
    finally:
        db.close()
    for row in rows:
        record = json.loads(row['data'])['trial']
        if not record.get('ready') or record.get('notified'):
            continue
        if not record.get('done'):
            tasks = [store.get(r['task']) for r in record['runs']]
            if any(t['status'] in regression.ACTIVE or store.lease_busy(t['id']) for t in tasks):
                continue
            cases = {c['id']: c for c in record['cases']}
            outcomes = []
            for run, task in zip(record['runs'], tasks):
                folder = store.directory(task['id'])
                answer = folder / 'result.txt'
                text = answer.read_text(encoding='utf-8') if answer.exists() else ''
                grade = regression.grade(None, cases[run['case']], text, folder / 'trial_output')
                if task['status'] != 'finished':
                    grade.update(passed=False, quality=0, note='Run did not finish: ' + task['status'])
                outcomes.append(dict(run, status=task['status'], grade=grade,
                                     usage=regression.generation_usage(folder), answer=str(answer)))
            record.update(done=True, finished=time.time(), outcomes=outcomes)
            (Path(record['folder']) / 'evidence.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
            path = Path(record['folder']) / 'report.md'
            path.write_text(render_report(record), encoding='utf-8')
            record['report'] = str(path)
            learning.set_trial(store.agent_dir, row['chat'], row['key'], record)
        delivered = send(row['chat'], Path(record['report']).read_text(encoding='utf-8'))
        if delivered is False:
            raise OSError('Lesson comparison was not delivered')
        record['notified'] = True
        learning.set_trial(store.agent_dir, row['chat'], row['key'], record)


def render_report(record):
    lines = ['Skill trial: paired screening, not proof of general improvement.',
             'Development and held-out scores stay separate. No automatic approval. Subjective checks remain unverified.',
             f"Allowance: one session, at most {record['max_steps']} steps; {record['repetitions']} repetitions. This is not a token ceiling."]
    if record.get('scheduling_error'):
        lines.append('INCONCLUSIVE: incomplete scheduling: ' + record['scheduling_error'])
    for case in record['cases']:
        lines.append(f"\n{record['splits'][case['id']]} — {case['id']}")
        for attempt in range(1, record['repetitions'] + 1):
            pair = [r for r in record['outcomes'] if r['case'] == case['id'] and r['attempt'] == attempt]
            for run in pair:
                u, g = run['usage'], run['grade']
                usage = f"input={u.get('input_total')}, cached={u.get('cache_read')}, output={u.get('output')}" if u.get('known') else 'usage unknown; no token-saving claim'
                lines.append(f"  {attempt} {run['arm']}: {run['status']}; quality={g['quality']:.2f}, all checks pass={g['passed']}; {usage}. Output: {run['answer']}")
            comparable = len(pair) == 2 and all(r['status'] == 'finished' and r['usage'].get('known') and r['usage'].get('routes') for r in pair)
            comparable = comparable and sorted(pair[0]['usage']['routes'], key=str) == sorted(pair[1]['usage']['routes'], key=str)
            if comparable:
                arms = {r['arm']: r for r in pair}
                a, b = arms['baseline'], arms['variant']
                lines.append(f"  Change: quality {b['grade']['quality'] - a['grade']['quality']:+.2f}; input tokens {b['usage']['input_total'] - a['usage']['input_total']:+}; output tokens {b['usage']['output'] - a['usage']['output']:+}.")
            else:
                lines.append('  INCONCLUSIVE: incomplete run or missing/mismatched measured route and usage.')
    lines.append('Review files and case relevance before deciding. No skill changed. Evidence: ' + record['folder'])
    return '\n'.join(lines)
