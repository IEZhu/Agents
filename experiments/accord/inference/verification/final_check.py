"""Recorded-run audit; run from repo root with PYTHONPATH=. and the retained local snapshot.

Unlike the portable offline verify module, this audit also checks snapshot bytes and a stopped server.
"""

import json
from pathlib import Path
import socket

from experiments.accord.inference.client import fingerprint
from experiments.accord.inference.format_control import control_summary
from experiments.accord.inference.run import hash_file
from experiments.accord.inference.verify import verify
from experiments.accord.inference.workload import load_workload, score

root=Path('experiments/accord/inference')
primary=root/'results/qwen3-8b-m4max-20261014'
control=root/'results/qwen3-8b-format-control-20261014'
checks=verify(primary)
setup=json.loads((primary/'setup.json').read_text())
plan=json.loads((control/'plan.json').read_text())
saved=json.loads((control/'summary.json').read_text())
all_rows=[json.loads(line) for line in (control/'trials.jsonl').read_text().splitlines()]
rows=[r for r in all_rows if not r['preconditioning']]
warmups=[r for r in all_rows if r['preconditioning']]
assert len(rows)==48 and len(warmups)==12 and len(all_rows)==60
assert plan['protocol_sha256']==hash_file(root/'format-control-protocol.md')
assert plan['harness_sha256']==hash_file(root/'format_control.py')
assert plan['primary_setup_sha256']==hash_file(primary/'setup.json')
assert plan['profile']==setup['profile']
expected=control_summary(rows)
expected['preconditioning_requests']=12
expected['preconditioning_operational_total_ms']=sum(r['operational_total_ms'] for r in warmups)
assert {k:v for k,v in saved.items() if k!='completed_at'}==expected
workload=load_workload();cases={c['name']:c for c in workload['cases']}
prepared={p['case']:p for p in setup['prepared']}
settings=set()
for block in range(12):
 group=all_rows[block*5:block*5+5]
 case=workload['cases'][plan['case_indices'][block]]
 order=['text_warm','ids_warm','ids_warm','text_warm'] if block%2==0 else ['ids_warm','text_warm','text_warm','ids_warm']
 assert [r['cell'] for r in group]==[order[0],*order]
 assert [r['position'] for r in group]==[-1,0,1,2,3]
 assert all(r['case']==case['name'] and r['block']==block for r in group)
for row in all_rows:
 p=prepared[row['case']]
 assert row['cache_n']==3752
 assert row['cache_n']+row['prompt_n']==p['input_tokens']==row['final']['tokens_evaluated']
 assert row['final']['prompt']==p['prompt']
 assert len(row['tokens'])==row['predicted_n']==row['final']['tokens_predicted']
 assert not row['invariant_errors'] and not row['final']['truncated']
 assert row['score']==score(row['content'],cases[row['case']]['expected'])
 settings.add(fingerprint(row['final']['generation_settings']))
assert len(settings)==1
assert next(iter(settings))==fingerprint(json.loads((primary/'trials.jsonl').read_text().splitlines()[0])['final']['generation_settings'])
snapshot=Path('.accord-local/slots')/setup['cache']['filename']
assert hash_file(snapshot)==setup['cache']['snapshot_sha256']
assert snapshot.stat().st_size==setup['cache']['bytes']
with socket.socket() as sock:
 sock.settimeout(1)
 server_stopped=sock.connect_ex(('127.0.0.1',18187))!=0
assert server_stopped
checks['format_control']={'status':'passed','scored_requests':48,'preconditioning_requests':12,
 'exact_order_verified':True,'token_invariants_and_scoring_verified':True,
 'effective_settings_equal_primary':True,'summary_recomputed':True,
 'artifact_sha256':{name:hash_file(control/name) for name in ('plan.json','trials.jsonl','summary.json')}}
checks['snapshot_sha256_unchanged_after_all_trials']=True
checks['server_stopped']=server_stopped
out=root/'verification/final-evidence-checks.json'
out.write_text(json.dumps(checks,indent=2)+'\n')
print(json.dumps(checks,indent=2))
