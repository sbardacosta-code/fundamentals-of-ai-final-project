#!/bin/zsh
# Recorded demo: interrupt a paid Jev checkpoint after saved progress, then resume.
# 50 fresh reviews (ranks 100,001-100,050, never processed), separate store, hard cap $0.05.
cd "$(dirname "$0")/.."
ROOT=artifacts/demo_resume
ARGS=(--input data/scale/demo_resume_50.csv --name demo_50 --root $ROOT --expected-rows 50 --budget-usd 0.05 --workers 2 --execute-paid)
echo "\n=== 1) Start: 50 new reviews, 2 parallel Jev workers, batch 5. Interrupt after 4 saved batches ==="
python3 -m review_pipeline.scale $ARGS --stop-after-batches 4
echo "\nSaved before interruption: $(python3 -c "import json;print(len(json.load(open('$ROOT/demo_50/checkpoint_before.json'))['completed_ids']))") completed reviews"
sleep 4
echo "\n=== 2) Resume the same command: already-completed reviews are not sent again ==="
python3 -m review_pipeline.scale $ARGS
python3 - <<PY
import json
r=json.load(open('$ROOT/demo_50/run.json'))
b=set(json.load(open('$ROOT/demo_50/checkpoint_before.json'))['completed_ids'])
a=set(json.load(open('$ROOT/demo_50/checkpoint_after.json'))['completed_ids'])
calls=[json.loads(l) for l in open('$ROOT/demo_50/calls.jsonl')]
resent=sum(1 for c in calls if c['role']=='enrich' and c['phase']=='resume' and b & set(c['review_ids']))
print(f"\nBefore: {len(b)} completed  ->  After: {len(a)} completed")
print(f"Resume enrichment calls that re-sent already completed reviews: {resent}")
print(f"Spent: \${r['ledger']['spent']:.6f} of \${r['budget_usd']} cap")
PY
echo "\n=== Demo finished ==="
