"""Explicit, bounded development trials sharing the original pilot spend ledger."""
import argparse
import fcntl
import json
from pathlib import Path
import time
import uuid

from . import compact_jev
from .jev import JevError, JevClient, build_request, MODEL
from .pilot import _usage, _write_lines, load_api_key
from .storage import Store, atomic_json, canonical, file_sha

EXPERIMENT = 'prompt-cost-quality-v1'
TRIAL_CAP = .25


def run_trial(variant, batch_size=5, pilot_dir='artifacts/pilot', output='artifacts/optimization', client=None, input_path=None):
    if variant not in ('compact', 'shared', 'shared_short', 'shared_rules', 'hybrid', 'calibrated', 'baseline') or batch_size not in (1, 5, 10, 20):
        raise ValueError('Unsupported declared trial')
    root, out = Path(pilot_dir), Path(output)
    out.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((root / 'manifest.json').read_text())
    run_id, dataset = manifest['run_id'], manifest['identity']['dataset_id']
    trial_name = f'{variant}-b{batch_size}'
    config = dict(role='enrich', model=MODEL, experiment=EXPERIMENT, variant=variant,
                  batch_size=batch_size, adapter_sha256=file_sha(compact_jev.__file__),
                  base_adapter_sha256=file_sha(Path(__file__).with_name('jev.py')))
    with (root / 'pilot.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store = Store(root / 'pilot.sqlite')
        started = time.monotonic()
        if input_path is not None:
            profile = store.ingest(input_path)
            if profile['counts']['records'] != 100:
                store.close()
                raise ValueError('Optimization evaluation sets must contain exactly 100 rows')
            dataset = profile['file_sha256']
            config['evaluation_dataset'] = dataset
        cid = store.prepare(dataset, config)
        prior_calls = store.db.execute('SELECT COUNT(*) FROM calls WHERE config_id=?', (cid,)).fetchone()[0]
        owner = 'optimization-' + uuid.uuid4().hex
        error = None
        try:
            ledger = store.db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if ledger['reserved'] > 1e-9:
                raise ValueError('Unresolved reservations require reconciliation')
            with store.transaction():
                store.db.execute("UPDATE jobs SET state='pending',owner=NULL,lease_until=NULL WHERE config_id=? AND state='claimed'", (cid,))
            engine = client or (JevClient() if variant == 'baseline' else compact_jev.CompactJevClient(variant=variant))
            while True:
                jobs = store.claim(cid, owner, batch_size=batch_size)
                if not jobs:
                    break
                all_calls = [json.loads(row[0]) for row in store.db.execute('SELECT payload FROM calls WHERE run_id=?', (run_id,))]
                experiment_calls = [c for c in all_calls if c.get('experiment') == EXPERIMENT]
                if any(c.get('cost_usd') is None for c in experiment_calls):
                    raise ValueError('Unknown trial charges block further dispatch')
                if sum(c['cost_usd'] for c in experiment_calls) + .01 > TRIAL_CAP:
                    raise ValueError('Optimization sub-budget would be exceeded')
                request = build_request(jobs) if variant == 'baseline' else compact_jev.build_compact_request(jobs, variant=variant)
                state_bytes = len(canonical(request['state']).encode())
                longest_question = max(len(canonical(q).encode()) for q in request['questions'].values())
                if len(canonical(request).encode()) > 60000 or state_bytes + longest_question > 30000:
                    raise ValueError('Request exceeds conservative context bound')
                call_id = store.dispatch(run_id, cid, owner, jobs, .01)
                with store.transaction():
                    payload = json.loads(store.db.execute('SELECT payload FROM calls WHERE id=?', (call_id,)).fetchone()[0])
                    payload.update(experiment=EXPERIMENT, trial=trial_name, run_phase='optimization', request=request,
                                   cached_tokens_reported=False, cached_input_tokens_policy='zero discount; no provider breakdown')
                    store.db.execute('UPDATE calls SET payload=? WHERE id=?', (canonical(payload), call_id))
                try:
                    results, raw = engine.evaluate(jobs)
                    usage, cost = _usage(raw)
                    if cost is None:
                        store.finish(call_id, error='Unknown usage', raw_response=raw, usage=usage)
                        raise RuntimeError('Unknown usage; reservation retained')
                    store.finish(call_id, results, usage=usage, cost=cost, raw_response=raw)
                    if cost > .01:
                        raise RuntimeError('Charge exceeded reservation; stop')
                except JevError as exc:
                    usage, cost = _usage(exc.raw_response)
                    store.finish(call_id, error=str(exc), usage=usage, cost=cost,
                                 raw_response=exc.raw_response, retry=cost is not None)
                    if cost is None:
                        raise
                _write_lines(out / (trial_name + '.jsonl'), store.records(dataset, cid))
        except BaseException as exc:
            error = str(exc)
            raise
        finally:
            rows = list(store.records(dataset, cid))
            _write_lines(out / (trial_name + '.jsonl'), rows)
            calls = [json.loads(row[0]) for row in store.db.execute('SELECT payload FROM calls WHERE run_id=? ORDER BY created,id', (run_id,))]
            calls = [c for c in calls if c.get('experiment') == EXPERIMENT]
            _write_lines(out / 'calls.jsonl', calls)
            own = [c for c in calls if c['label_config'] == cid]
            result = dict(trial=trial_name, config=config, config_id=cid, input_sha256=dataset,
                          complete=all(r['status']=='completed' for r in rows), count=len(rows),
                          unique_enrichment_count=len({rid for call in own for rid in call.get('review_ids', [])}),
                          attempts=len(own), cost_usd=sum(c.get('cost_usd') or 0 for c in own),
                          unknown_cost_attempts=sum(c.get('cost_usd') is None for c in own),
                          input_tokens=sum(c.get('input_tokens',0) for c in own),
                          elapsed_seconds=time.monotonic()-started, error=error,
                          ledger=dict(store.db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()))
            result['known_cost_subtotal_usd'] = result['cost_usd']
            if result['unknown_cost_attempts']:
                result['cost_usd'] = None
            result['new_attempts'] = len(own) - prior_calls
            metadata_path = out / (trial_name + '-meta.json')
            if result['new_attempts'] == 0 and metadata_path.exists() and result['complete']:
                result['cache_replay'] = True
                atomic_json(out / (trial_name + '-cache-replay.json'), result)
            else:
                atomic_json(metadata_path, result)
            store.close()
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--variant', choices=['compact','shared','shared_short','shared_rules','hybrid','calibrated','baseline'], required=True)
    p.add_argument('--batch-size', type=int, default=5)
    p.add_argument('--execute-paid', action='store_true')
    p.add_argument('--input')
    p.add_argument('--output', default='artifacts/optimization')
    args = p.parse_args()
    if not args.execute_paid:
        p.error('Explicit --execute-paid required; shared $5 limit and $.25 trial sublimit apply')
    load_api_key()
    print(json.dumps(run_trial(args.variant,args.batch_size,output=args.output,input_path=args.input), indent=2))


if __name__ == '__main__':
    main()
