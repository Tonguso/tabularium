"""Explicit live subscription probe: python tests/ordinary_workflow_probe.py LABEL. Not an offline unittest.

Two stages use ordinary completion, not the trusted benchmark oracle. No outward actions or notifications.
"""
import json, sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from tabularium.learning import regression
from tabularium.records import verification
from tabularium.core.roster import ROLES
from tabularium.records.background_runtime import TaskStore
from tabularium.channels.owner_telegram import OWNER_CHAT_ID

ROOT = Path(__file__).resolve().parents[1]

def run(name):
    store = TaskStore(ROOT / name, ROLES[name])
    brief = ('Synthetic ordinary-workflow test. Write towers.txt containing exactly towers in your trial_output folder. '
             'Declare an executable contains check for the saved file and verify it before completing. '
             'No research, personal history or external actions. Use only these synthetic files. Keep the reply short.')
    if '--eval-lane' in sys.argv:
        brief = '[Eval ordinary-proof-continuation] ' + brief
    row = store.start(OWNER_CHAT_ID, brief, extra={'eval': {'case': 'ordinary-proof-continuation'}, 'notify_telegram': False},
                      completion_items=[dict(item='Write towers.txt containing towers', check='Read the saved towers file', critical=True)])
    ident = row['id']; folder = store.directory(ident)
    first = regression.wait(store, ident, timeout=240)
    initial = dict(status=first, usage=regression.generation_usage(folder), proof=verification.status(store, ident, '', 1))
    if first == 'finished':
        store.control(OWNER_CHAT_ID, ident, 'resume', 'Also write carriers.txt containing exactly carriers in your trial_output folder. Keep towers.txt unchanged. Declare and verify this added obligation using its source instruction ID. Complete both requirements; no external actions.')
        final = regression.wait(store, ident, timeout=240)
    else:
        final = first
    files = {n: (folder/'trial_output'/f'{n}.txt').read_text().strip() if (folder/'trial_output'/f'{n}.txt').exists() else None for n in ('towers','carriers')}
    actions = []
    for p in sorted((folder/'task_state'/'actions').glob('*.json')):
        value = json.loads(p.read_text())
        actions.append(dict(tool=value['tool'], inputs=value.get('inputs'), result=value.get('result')))
    record = dict(agent=name, task=ident, first=initial, status=final, files=files, usage=regression.generation_usage(folder), actions=actions, release=store.get(ident).get('release'))
    (ROOT / 'coordinator/scratch' / f'ordinary-{sys.argv[1]}-{name}.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
    print(json.dumps({k:v for k,v in record.items() if k!='actions'}), flush=True)
    return record
pool = ThreadPoolExecutor(max_workers=4)
try:
    records = list(pool.map(run, [arg for arg in sys.argv[2:] if arg != '--eval-lane'] or ROLES))
finally:
    pool.shutdown()
(ROOT / 'coordinator/scratch' / f'ordinary-{sys.argv[1]}.json').write_text(json.dumps(records, indent=2), encoding='utf-8')
