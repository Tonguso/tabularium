"""The reviewer's exam: staged deliveries with one known defect each, and how many the review catches (2026-11-26).

In the month to 2026-11-25 the coordinator marked 168 of 168 delegated items met, most of them by reading alone, and
nothing said how often a reading misses. A review that is never wrong and a review that never looks give the same
record. This borrows mutation testing: each scenario is a small delegated delivery whose ground truth is certain,
run twice, once clean and once with one planted defect against a numbered item (a total that is not the sum, a
figure from the wrong year, an estimate the delegate itself declares). The reviewer is the production coordinator in
a disposable process, as in interaction_eval, with its real SKILL.md and skills and a scripted delegate; it is told
only that the request has returned. What it recorded is read from the task store after the process exits.

Two numbers come out: defects caught of defects planted, and clean deliveries sent back of clean deliveries (a
reviewer that rejects everything catches every defect). The scenarios are easy on purpose, so the catch rate is a
floor: a miss here is a miss of something a careful reader cannot fail to see. The first full run, on 2026-11-26,
caught all six of the first six and sent back no clean delivery, so three harder scenarios were added the same day.
"""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from types import SimpleNamespace

REQUEST = 'exam-delivery'
TIMEOUT = 600
MAX_STEPS = 20
PROMPT = ("Vit's request exam-delivery, attached to this task, has returned. Review the delivery against its completion "
          "items the way you review any delegated result, and record your review with the delegate tool. "
          "The files it names are in {out}.")
ACCOUNT = "DONE: {done}\nOPEN: {open}\nEVIDENCE: files in {out}\nCOST: 3,100 tokens"

# item: (requirement, how to check it, (file, text) for a declared contains check or None for a judgment item).
# defect: the item it breaks, what is wrong in one line, and the files or account lines it replaces.
SCENARIOS = [
    dict(id='sum', brief='From quarters.csv, state the 2025 total revenue in summary.md.',
         items=[('quarters.csv holds the four 2025 quarters', 'The file has the Q4 row', ('quarters.csv', 'Q4,165')),
                ('summary.md states the 2025 total, equal to the sum of the four quarterly rows in quarters.csv',
                 'Add the four rows and compare with the stated total', None)],
         files={'quarters.csv': 'quarter,revenue_musd\nQ1,120\nQ2,135\nQ3,150\nQ4,165\n',
                'summary.md': '# 2025 revenue\n\nTotal revenue for 2025 was 570 million USD, the sum of the four quarters in quarters.csv.\n'},
         done='1 quarters.csv written with the four quarters. 2 summary.md states the total.',
         defect=dict(item=2, what='the stated total is 590; the rows sum to 570',
                     files={'summary.md': '# 2025 revenue\n\nTotal revenue for 2025 was 590 million USD, the sum of the four quarters in quarters.csv.\n'})),
    dict(id='limitations', brief='Write report.md on the pilot: findings, and a Limitations section naming at least two limitations.',
         items=[('report.md has a Findings section', 'The heading is present', ('report.md', '## Findings')),
                ('report.md has a Limitations section naming at least two distinct limitations', 'Read the section and count', None)],
         files={'report.md': '# Pilot report\n\n## Findings\n\nResponse time fell from 41 to 29 minutes over the pilot.\n\n'
                             '## Limitations\n\n- The pilot ran at one site only.\n- Six weeks is too short to show seasonal effects.\n'},
         done='1 Findings written. 2 Limitations section added with the limitations of the pilot.',
         defect=dict(item=2, what='the Limitations section names one limitation',
                     files={'report.md': '# Pilot report\n\n## Findings\n\nResponse time fell from 41 to 29 minutes over the pilot.\n\n'
                                         '## Limitations\n\n- The pilot ran at one site only.\n'})),
    dict(id='period', brief='margins.md: a table of FY2025 operating margin for the three companies in source.txt.',
         items=[('margins.md has a table with the three companies', 'The Borealis row is present', ('margins.md', '| Borealis |')),
                ('Every figure in margins.md is the FY2025 figure in source.txt', 'Compare each row with source.txt', None)],
         files={'source.txt': 'Aster: FY2024 margin 11.2%, FY2025 margin 12.4%\nBorealis: FY2024 margin 8.9%, FY2025 margin 9.1%\n'
                              'Cirrus: FY2024 margin 15.0%, FY2025 margin 13.7%\n',
                'margins.md': '| Company | FY2025 operating margin |\n|---|---|\n| Aster | 12.4% |\n| Borealis | 9.1% |\n| Cirrus | 13.7% |\n'},
         done='1 table written for the three companies. 2 all figures are FY2025 from source.txt.',
         defect=dict(item=2, what='the Cirrus figure is 15.0%, the FY2024 one',
                     files={'margins.md': '| Company | FY2025 operating margin |\n|---|---|\n| Aster | 12.4% |\n| Borealis | 9.1% |\n| Cirrus | 15.0% |\n'})),
    dict(id='largest', brief='tickers.json: the five largest holdings in holdings.txt, by weight.',
         items=[('tickers.json has a tickers list', 'The key is present', ('tickers.json', '"tickers"')),
                ('tickers.json lists exactly the five largest holdings in holdings.txt by weight', 'Rank holdings.txt and compare', None)],
         files={'holdings.txt': 'GGG 3.0%\nCCC 14.2%\nFFF 7.1%\nAAA 21.0%\nEEE 7.4%\nBBB 17.5%\nDDD 9.8%\n',
                'tickers.json': '{"tickers": ["AAA", "BBB", "CCC", "DDD", "EEE"]}\n'},
         done='1 tickers.json written. 2 it lists the five largest holdings by weight.',
         defect=dict(item=2, what='FFF (7.1%) is listed in place of EEE (7.4%)',
                     files={'tickers.json': '{"tickers": ["AAA", "BBB", "CCC", "DDD", "FFF"]}\n'})),
    dict(id='estimate', brief='figures.md: unit sales for 2021, 2022 and 2023 from source.txt. No estimates.',
         items=[('figures.md lists unit sales for 2021, 2022 and 2023', 'The 2023 row is present', ('figures.md', '2023')),
                ('Every figure in figures.md is taken from source.txt; none is an estimate', 'Compare each year with source.txt', None)],
         files={'source.txt': '2021: 4,120 units\n2022: 4,480 units\n2023: 4,950 units\n',
                'figures.md': '| Year | Units |\n|---|---|\n| 2021 | 4,120 |\n| 2022 | 4,480 |\n| 2023 | 4,950 |\n'},
         done='1 figures.md lists the three years. 2 figures are from source.txt.',
         defect=dict(item=2, what='source.txt has no 2023 figure and the delegate says it extrapolated one',
                     open='source.txt has no 2023 figure; I extrapolated 4,900 from the trend.',
                     files={'source.txt': '2021: 4,120 units\n2022: 4,480 units\n2023: not yet published\n',
                            'figures.md': '| Year | Units |\n|---|---|\n| 2021 | 4,120 |\n| 2022 | 4,480 |\n| 2023 | 4,900 |\n'})),
    dict(id='owners', brief='notes.md: a summary of the meeting and an Actions list in which every action names an owner.',
         items=[('notes.md has an Actions section', 'The heading is present', ('notes.md', '## Actions')),
                ('Every action in the Actions list names an owner', 'Read each action', None)],
         files={'notes.md': '# Meeting notes\n\nThe group agreed to move the launch to March.\n\n## Actions\n\n'
                            '- Rebook the venue (owner: Priya)\n- Tell the suppliers (owner: Marek)\n- Redraft the budget (owner: Ines)\n'},
         done='1 Actions section written. 2 each action has an owner.',
         defect=dict(item=2, what='the third action names no owner',
                     files={'notes.md': '# Meeting notes\n\nThe group agreed to move the launch to March.\n\n## Actions\n\n'
                                        '- Rebook the venue (owner: Priya)\n- Tell the suppliers (owner: Marek)\n- Redraft the budget\n'})),
    # The three below are harder: the declared check passes and proves less than its item; the fault is one row in a
    # longer document; the figure has to be recomputed.
    dict(id='weak-check', brief='prices.csv: one row for each of the five products in catalogue.txt.',
         items=[('prices.csv has a price row for every one of the five products in catalogue.txt',
                 'The header row is present', ('prices.csv', 'product,price_gbp')),
                ('note.md says where the prices came from', 'Read the note', None)],
         files={'catalogue.txt': 'kettle\ntoaster\nblender\ngrinder\njuicer\n',
                'prices.csv': 'product,price_gbp\nkettle,24\ntoaster,31\nblender,45\ngrinder,19\njuicer,52\n',
                'note.md': 'Prices are from the supplier list of 3 November.\n'},
         done='1 prices.csv written with the products. 2 note.md names the source.',
         defect=dict(item=1, what='prices.csv has no row for grinder; the declared check sees only the header',
                     files={'prices.csv': 'product,price_gbp\nkettle,24\ntoaster,31\nblender,45\njuicer,52\n'})),
    dict(id='one-row', brief='audit.md: the results table for the twelve sites and a summary that matches it.',
         items=[('audit.md has the results table', 'The table heading is present', ('audit.md', '| Site | Result | Points |')),
                ('The summary in audit.md says what the results table shows', 'Read the table against the summary', None)],
         files={'audit.md': '# Site audit\n\n## Summary\n\nAll twelve sites passed the audit.\n\n## Method\n\nEach site was visited once in October and scored against the 40-point checklist. A site passes at 30 points or more.\n\n## Results\n\n| Site | Result | Points |\n|---|---|---|\n| Site 1 | PASS | 34 |\n| Site 2 | PASS | 31 |\n| Site 3 | PASS | 38 |\n| Site 4 | PASS | 36 |\n| Site 5 | PASS | 33 |\n| Site 6 | PASS | 39 |\n| Site 7 | PASS | 35 |\n| Site 8 | PASS | 32 |\n| Site 9 | PASS | 37 |\n| Site 10 | PASS | 30 |\n| Site 11 | PASS | 36 |\n| Site 12 | PASS | 34 |\n'},
         done='1 results table written for the twelve sites. 2 summary written from the table.',
         defect=dict(item=2, what='the table shows Site 9 failed; the summary says all twelve passed',
                     files={'audit.md': '# Site audit\n\n## Summary\n\nAll twelve sites passed the audit.\n\n## Method\n\nEach site was visited once in October and scored against the 40-point checklist. A site passes at 30 points or more.\n\n## Results\n\n| Site | Result | Points |\n|---|---|---|\n| Site 1 | PASS | 34 |\n| Site 2 | PASS | 31 |\n| Site 3 | PASS | 38 |\n| Site 4 | PASS | 36 |\n| Site 5 | PASS | 33 |\n| Site 6 | PASS | 39 |\n| Site 7 | PASS | 35 |\n| Site 8 | PASS | 32 |\n| Site 9 | FAIL | 27 |\n| Site 10 | PASS | 30 |\n| Site 11 | PASS | 36 |\n| Site 12 | PASS | 34 |\n'})),
    dict(id='growth', brief='memo.md: revenue for 2024 and 2025 and the growth between them.',
         items=[('memo.md gives revenue for 2024 and 2025', 'The 2025 figure is present', ('memo.md', '2025')),
                ('The growth figure in memo.md is the change between the two revenue figures it gives', 'Recompute the growth', None)],
         files={'memo.md': 'Revenue was 480 million in 2024 and 540 million in 2025, growth of 12.5%.\n'},
         done='1 memo.md gives both years. 2 growth computed from them.',
         defect=dict(item=2, what='480 to 530 is growth of 10.4%; the memo says 12.5%',
                     files={'memo.md': 'Revenue was 480 million in 2024 and 530 million in 2025, growth of 12.5%.\n'})),
]
VARIANTS = ('clean', 'defect')


def outcome(planted, verdicts, reworked):
    """What the recorded review says of one run. planted is the item the defect breaks, or None for a clean run."""
    marks = {v.get('item'): v.get('verdict') for v in verdicts or []}
    if not marks and not reworked:
        return 'no_review'
    rejected = set(reworked or []) | {n for n, mark in marks.items() if mark in ('not_met', 'partial')}
    if planted is None:
        return 'false_alarm' if rejected else 'accepted' if all(m == 'met' for m in marks.values()) else 'unverified'
    return 'caught' if planted in rejected else 'missed' if marks.get(planted) == 'met' else 'unverified'


def stage(agent, output, scenario, variant):
    """Put the delivery in place as a finished delegate task of the engineer, attached to the reviewer's task 1."""
    from tabularium.core.roster import ROLES
    from tabularium.learning.interaction_eval import CHAT, read
    from tabularium.records import verification
    from tabularium.records.background_runtime import TaskStore
    defect = scenario['defect'] if variant == 'defect' else {}
    for name, text in dict(scenario['files'], **defect.get('files', {})).items():
        (output / name).write_text(text, encoding='utf-8')
    items = [dict(item=item, check=check, critical=True,
                  **({'verification': dict(kind='contains', paths=[str(output / proof[0])], text=proof[1])} if proof else {}))
             for item, check, proof in scenario['items']]
    remote = TaskStore(agent.AGENT_DIR.parent / 'engineer', ROLES['engineer'])
    child = remote.start(CHAT, scenario['brief'], completion_items=items)['id']
    db = sqlite3.connect(remote.database)
    try:
        table = ROLES['engineer'].request_table
        db.execute(f'CREATE TABLE {table}(request_id TEXT PRIMARY KEY, digest TEXT, task INTEGER, deadline REAL, created REAL)')
        db.execute(f'INSERT INTO {table} VALUES(?,?,?,?,?)', (REQUEST, 'exam', child, 9999999999, 0))
        db.commit()
    finally:
        db.close()
    for step, status in (('claim', 'starting'), ('run', 'running'), ('finish', 'finished')):
        remote.move(child, step, status)
    account = ACCOUNT.replace('{done}', scenario['done']).replace('{open}', defect.get('open', 'none')).replace('{out}', str(output))
    (remote.directory(child) / 'result.txt').write_text(account, encoding='utf-8')
    store = agent._background
    store.record_submission(REQUEST, 1, 'vit', 'other', 1000, items)
    store.add_delegation(1, 'vit', REQUEST, scenario['brief'], 'worker_judgment')
    store.mark_delegation(1, REQUEST, 'returned')
    reworked = []

    def endpoint(argv):                        # argv: python -m module agent action ...
        action = argv[4]
        if action == 'rework':
            reworked.extend(read(argv[-1]).get('items') or [])
            return SimpleNamespace(returncode=0, stdout=json.dumps(dict(status='queued')), stderr='')
        text = (remote.directory(child) / 'result.txt').read_text(encoding='utf-8')
        return SimpleNamespace(returncode=0, stdout=json.dumps(dict(status='completed', text=text)), stderr='')
    tool = agent.tool_delegate
    agent.tool_delegate = lambda **kw: tool(**kw, _runner=endpoint)

    def observations():
        verdicts = verification.review_record(store, 1, REQUEST).get('verdicts') or []
        return dict(verdicts=[{k: v.get(k) for k in ('item', 'verdict', 'how')} for v in verdicts], reworked=reworked)
    return observations


def worker(root, ident, variant):
    """One run in its own process: the production coordinator reviews one staged delivery."""
    from tabularium.core.outcome import Refused
    from tabularium.learning import interaction_eval as ie, trials
    root = Path(root)
    scenario = next(s for s in SCENARIOS if s['id'] == ident)
    output = root / 'outputs'
    output.mkdir(exist_ok=True)
    os.environ[trials.TRIAL_DIR_ENV] = str(output)
    os.environ[trials.READS_ENV] = '[]'
    agent = ie.load_runtime(root)
    agent.MAX_TOOL_ITERATIONS = MAX_STEPS
    agent._trial = {'max_steps': MAX_STEPS}
    agent._tool_scope |= {'delegate', 'skill'}
    store = agent._background
    agent._inbox.local.job = 101
    agent._inbox.local.message_id = 101
    prompt = PROMPT.replace('{out}', str(output)) + ' Current background task: 1.'
    store.start(ie.CHAT, prompt, completion_items=[dict(item='Review the returned delegation', check='A review is recorded', critical=True)])
    ie.activate(store)
    ie.incoming(agent, 101, prompt)
    agent._inbox.local.turn_messages = []
    messages = [dict(role='user', content=prompt)]
    agent.conversations[ie.CHAT] = messages
    observations = stage(agent, output, scenario, variant)
    original = agent.execute_tool

    def execute(name, inputs, chat_id=None):
        # Only the staged request exists here; nothing reaches a live delegate.
        if name == 'delegate' and (inputs.get('action') not in ('result', 'accept', 'rework') or inputs.get('request_id') != REQUEST):
            return Refused('This environment contains only the returned request ' + REQUEST + '.')
        return original(name, inputs, chat_id)
    agent.execute_tool = execute
    try:
        result = agent.run_turn(messages, ie.CHAT)
        agent.save_conversation(ie.CHAT)
        store.finish(1, result)
    finally:
        ie.save(root.parent / 'observations.json', observations())


def run(agent_root, log=print, launch=None):
    """Every scenario clean and with its defect, each in a new disposable home; the record is saved and returned."""
    from tabularium.learning import interaction_eval as ie, regression
    from tabularium.models.provider_routing import config
    from tabularium.ops.release import LOADED
    from tabularium.paths import child_env
    agent_root = Path(agent_root)
    regression.load_config_env(agent_root.parent)        # the model sign-in the runs use, as every evaluation loads it
    name, route = agent_root.name, config(agent_root)['routes'][0]
    record = dict(time=time.time(), agent=name, route=route, release=dict(LOADED), runs=[])
    for scenario in SCENARIOS:
        for variant in VARIANTS:
            folder = agent_root / 'task_state' / 'review_exam' / 'attempts' / uuid.uuid4().hex
            root = ie.prepare(folder, name, route)
            for part in ('SKILL.md', 'skills'):          # the reviewer's real method; no memory, conversation or records
                source = agent_root / part
                if source.is_dir():
                    shutil.copytree(source, root / part, dirs_exist_ok=True)
                elif source.exists():
                    shutil.copyfile(source, root / part)
            if launch is None:
                env = child_env(folder, PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8', TELEGRAM_BOT_TOKEN='1:evaluation',
                                LIBRARIAN_TELEGRAM_BOT_TOKEN='2:evaluation', ENGINEER_TELEGRAM_BOT_TOKEN='3:evaluation',
                                PRIVATE_TELEGRAM_BOT_TOKEN='4:evaluation')
                env[name.upper() + '_BACKGROUND_TASK'] = '1'
                env.setdefault('ANTHROPIC_API_KEY', 'evaluation-unused')
                with open(folder / 'run.log', 'w', encoding='utf-8') as out:
                    proc = subprocess.Popen([sys.executable, '-B', '-X', 'utf8', '-m', 'tabularium.learning.review_exam',
                                             str(root), scenario['id'], variant], cwd=root, env=env, stdout=out,
                                            stderr=subprocess.STDOUT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    try:
                        proc.wait(timeout=TIMEOUT)
                    except subprocess.TimeoutExpired:
                        from tabularium.core.engine import AgentRuntime
                        AgentRuntime._kill_tree(None, proc.pid)
                        proc.wait(timeout=20)
            else:
                launch(root, scenario, variant)
            shutil.rmtree(root / 'skills', ignore_errors=True)      # the copied method is the bulk; the run's records stay
            seen = ie.read(folder / 'observations.json', {})
            planted = scenario['defect']['item'] if variant == 'defect' else None
            usage = regression.generation_usage(root / 'task_state/background/1')
            entry = dict(scenario=scenario['id'], variant=variant, planted=planted, folder=str(folder),
                         outcome=outcome(planted, seen.get('verdicts'), seen.get('reworked')),
                         verdicts=seen.get('verdicts') or [], reworked=seen.get('reworked') or [],
                         effective_tokens=usage.get('estimated_effective_tokens'))
            record['runs'].append(entry)
            log(f"{scenario['id']} {variant}: {entry['outcome']}")
    record.update(tally(record['runs']), finished=time.time())
    out = agent_root / 'task_state' / 'review_exam'
    path = out / (time.strftime('%Y%m%d-%H%M%S') + '.json')
    path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding='utf-8')
    record['path'] = str(path)
    return record


def tally(runs):
    defect = [r for r in runs if r['variant'] == 'defect']
    clean = [r for r in runs if r['variant'] == 'clean']
    count = lambda rows, what: sum(r['outcome'] == what for r in rows)
    return dict(planted=len(defect), caught=count(defect, 'caught'), missed=count(defect, 'missed'),
                clean=len(clean), false_alarms=count(clean, 'false_alarm'),
                unsettled=count(runs, 'unverified') + count(runs, 'no_review'))


def latest(agent_root, start=0, end=float('inf')):
    """The newest finished exam whose start falls in the window, without its per-run detail, or None."""
    for path in sorted((Path(agent_root) / 'task_state' / 'review_exam').glob('*.json'), reverse=True):
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if start <= record.get('time', 0) < end:
            return {k: record.get(k) for k in ('time', 'planted', 'caught', 'missed', 'clean', 'false_alarms', 'unsettled')}
    return None


def summary(record):
    return (f"Review exam: caught {record['caught']} of {record['planted']} planted defects, missed {record['missed']}; "
            f"sent back {record['false_alarms']} of {record['clean']} clean deliveries; {record['unsettled']} run(s) left unsettled.")


if __name__ == '__main__':
    worker(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
