"""Write the contract's grading/ folder for the declared 100,000-review scope. Offline.

Records, membership, ranking, claims and memo come from the final checkpoint
folder. calls.jsonl merges every logged attempt from all nested checkpoint
ledgers in the shared store, because records reused from earlier checkpoints
were classified by those earlier calls. Resume evidence comes from the 500
checkpoint, which was deliberately interrupted and resumed.
"""
import argparse
import gzip
import json
import shutil
from pathlib import Path

from .storage import Store, atomic_json, canonical, file_sha

VERSION = 'a5-audit-v1'
CALL_FIELDS = ('request_id', 'role', 'review_ids', 'model', 'phase', 'outcome', 'label_config',
               'input_tokens', 'output_tokens', 'cached_input_tokens', 'cost_usd', 'run_id',
               'started_at', 'ended_at', 'worker', 'error')


def export(final='sample_100000', resume='checkpoint_500', checkpoints=('checkpoint_500', 'analysis_10000', 'sample_100000'),
           root='artifacts/scale', analysis='data/scale/sample_100000.csv', output='grading'):
    root, out = Path(root), Path(output)
    out.mkdir(parents=True, exist_ok=True)
    final_dir, resume_dir = root / final, root / resume
    manifest = json.loads((final_dir / 'manifest.json').read_text())
    if not manifest.get('stages_complete'):
        raise ValueError('Final checkpoint is incomplete')
    with gzip.open(out / 'records.jsonl.gz', 'wt', encoding='utf-8') as handle:
        for line in (final_dir / 'records.jsonl').read_text().splitlines():
            row = json.loads(line)
            row.pop('attempt_count', None)
            handle.write(canonical(row) + '\n')
    for name in ('membership.csv', 'ranking.csv', 'claims.csv', 'memo.json'):
        shutil.copyfile(final_dir / name, out / name)
    shutil.copyfile(resume_dir / 'checkpoint_before.json', out / 'checkpoint_before.json')
    shutil.copyfile(final_dir / 'checkpoint_after.json', out / 'checkpoint_after.json')
    run_ids = [json.loads((root / name / 'manifest.json').read_text())['run_id'] for name in checkpoints]
    store = Store(root / 'scale.sqlite')
    try:
        memo_runs = [row[0] for row in store.db.execute(
            'SELECT run_id FROM scale_memo_jobs WHERE folder LIKE ?', ('%' + final,))]
        counts = {}
        with gzip.open(out / 'calls.jsonl.gz', 'wt', encoding='utf-8') as handle:
            for run_id in run_ids + memo_runs:
                for (payload,) in store.db.execute('SELECT payload FROM calls WHERE run_id=? ORDER BY created,id', (run_id,)):
                    call = json.loads(payload)
                    if call['outcome'] not in ('succeeded', 'failed'):
                        raise ValueError('Unresolved call in ledger: ' + call['request_id'])
                    handle.write(canonical({k: call[k] for k in CALL_FIELDS if k in call}) + '\n')
                    counts[call['role']] = counts.get(call['role'], 0) + 1
    finally:
        store.close()
    analysis_rows = manifest['input_rows']
    atomic_json(out / 'run.json', {
        'version': VERSION, 'analysis_count': analysis_rows, 'analysis_sha256': file_sha(analysis),
        'classification_input_fields': ['review_text'], 'allow_multi_issue': False,
        'declared_scope': 'Deterministic 100,000-review sample per instructor update of 2026-10-11; not the full 660,622-row corpus.',
        'sample_method': 'First 100,000 rows by SHA-256(berkeley-fall-2026-assignment-5-v1:review_id) over unique nonempty valid-rating rows (instructor seed and ordering).',
        'enrichment_config': manifest['identity']['enrichment_config'],
        'checkpoint_ledgers': dict(zip(checkpoints, run_ids)), 'memo_ledgers': memo_runs,
        'call_counts_by_role': counts, 'raw_full_call_logs': 'cost/scale/<checkpoint>/calls.jsonl.gz',
    })
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='grading')
    args = parser.parse_args()
    print(json.dumps(export(output=args.output), indent=2))


if __name__ == '__main__':
    main()
