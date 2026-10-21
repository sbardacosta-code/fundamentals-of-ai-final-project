"""Explicit live 100-row Jev pilot. Imports and --help make no model calls."""
import argparse
import csv
import fcntl
import json
import os
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .cli import config_for
from .grouping import (INITIAL_CATALOG, CATALOG_VERSION, GROUP_PROMPT_VERSION,
                       JevGroupingClient, build_group_request)
from .jev import JevClient, JevError, build_request
from .ranking import rank
from .storage import Store, FIELDS, atomic_json, canonical, digest, file_sha

BUDGET = 5.0
RESERVATION = .01  # >64,000 input tokens at $0.042/M; outputs are free.
INPUT_RATE = Decimal('0.042')


def _usage(raw):
    """Recognize only documented token usage. Unknown billing must stop dispatch."""
    usage = raw.get('usage') if isinstance(raw, dict) else None
    if not isinstance(usage, dict):
        return None, None
    if any(type(usage.get(k)) is not int or usage[k] < 0 for k in ('input_tokens', 'output_tokens')):
        return usage, None
    amount = float(Decimal(usage['input_tokens']) * INPUT_RATE / Decimal(1000000))
    return dict(usage, cached_input_tokens=0), amount


def _write_lines(path, rows):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with tmp.open('w', encoding='utf-8') as handle:
        for row in rows:
            handle.write(canonical(row) + '\n')
    tmp.replace(path)


def _subset(store, root, name, sources):
    path = root / (name + '_input.csv')
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows([{k: row[k] for k in FIELDS} for row in sources])
    return store.ingest(path)['file_sha256']


def _export(store, root, dataset, config, run_id):
    records = list(store.records(dataset, config))
    _write_lines(root / 'pilot_records.jsonl', records)
    calls = [json.loads(row[0]) for row in store.db.execute('SELECT payload FROM calls WHERE run_id=? ORDER BY created,id', (run_id,))]
    calls = [call for call in calls if not call.get('experiment')]
    _write_lines(root / 'pilot_calls.jsonl', calls)
    atomic_json(root / 'checkpoint.json', store.snapshot(dataset, config))
    return records, calls


def run_pilot(input_path='data/source/cost_100.csv', directory='artifacts/pilot', phase='cold',
              stop_after_batches=None, enrich_client=None, group_client=None):
    """Run/resume queued stages under one persistent $5 ledger, never a fresh cap.

    Warm requires all Jev stages complete and makes zero provider calls. Memo is
    intentionally not claimed complete here; it needs its separate provider.
    """
    if phase not in ('cold', 'warm'):
        raise ValueError('phase must be cold or warm')
    if stop_after_batches is not None and (type(stop_after_batches) is not int or stop_after_batches < 1):
        raise ValueError('stop_after_batches must be a positive integer')
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'pilot.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another pilot process owns this directory') from None
        return _run_locked(Path(input_path), root, phase, stop_after_batches, enrich_client, group_client)


def _run_locked(input_path, root, phase, stop_after, enrich_client, group_client):
    started = time.monotonic()
    store = Store(root / 'pilot.sqlite')
    manifest_path = root / 'manifest.json'
    meta_path = root / (phase + '.json')
    batches = 0
    stage_times = {}
    active_stage = [None, None]
    stages = []
    error = None
    dataset = cid = run_id = None
    manifest = None
    try:
        config = config_for()
        profile = store.ingest(input_path)
        if profile['counts']['records'] != 100:
            raise ValueError('The fixed pilot requires exactly 100 source rows')
        dataset = profile['file_sha256']
        cid = store.prepare(dataset, config)
        grouping_config = dict(role='group', model=config['model'], prompt_version=GROUP_PROMPT_VERSION,
                               catalog=INITIAL_CATALOG, catalog_version=CATALOG_VERSION,
                               adapter_sha256=file_sha(Path(__file__).with_name('grouping.py')),
                               enrichment_config=cid)
        identity = dict(dataset_id=dataset, enrichment_config=cid,
                        verification_config=digest(canonical(config_for('verify'))),
                        grouping_config=digest(canonical(grouping_config)))
        resuming = manifest_path.exists()
        if resuming:
            manifest = json.loads(manifest_path.read_text())
            if manifest['identity'] != identity:
                raise ValueError('Pilot input/config changed; preserve existing ledger and request an explicit migration')
            run_id = manifest['run_id']
        else:
            if store.db.execute('SELECT COUNT(*) FROM runs').fetchone()[0]:
                raise ValueError('Existing ledger lacks manifest; reconcile it instead of resetting spending')
            if phase == 'warm':
                raise ValueError('Run cold before warm')
            run_id = store.create_run(BUDGET)
            # Predeclare blind verifier sample before observing any predictions.
            sources = [json.loads(row[0]) for row in store.db.execute('SELECT r.source_json FROM reviews r JOIN dataset_rows d USING(review_id) WHERE d.dataset_id=? AND r.empty=0', (dataset,))]
            selected = sorted(sources, key=lambda r: digest('pilot-verify-v1:' + r['review_id']))[:10]
            manifest = dict(identity=identity, run_id=run_id, budget_usd=BUDGET,
                            verification_ids=[r['review_id'] for r in selected], created_at=datetime.now(timezone.utc).isoformat(),
                            jev_stages_complete=False)
            atomic_json(manifest_path, manifest)
        ledger = store.db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if not ledger or ledger['budget'] != BUDGET:
            raise ValueError('Persistent budget ledger is missing or altered')
        if ledger['reserved'] > 1e-10:
            raise ValueError('Unresolved in-flight/unknown charges require reconciliation before more calls')
        if phase == 'warm' and not manifest.get('jev_stages_complete'):
            raise ValueError('Cold Jev stages must finish before warm')
        sources = {row['review_id']: json.loads(row['source_json']) for row in store.db.execute('SELECT r.* FROM reviews r JOIN dataset_rows d USING(review_id) WHERE d.dataset_id=?', (dataset,))}
        stage_times['prepare'] = time.monotonic() - started
        stages.append('prepare')
        enrich_client = enrich_client or JevClient()
        group_client = group_client or JevGroupingClient()
        owner = 'pilot-' + uuid.uuid4().hex

        def stage(stage_name, stage_dataset, stage_config, predictions=None):
            nonlocal batches
            begin = time.monotonic()
            active_stage[:] = [stage_name, begin]
            stage_cid = store.prepare(stage_dataset, stage_config)
            # Exclusive process lock means undispatched leftovers can be reclaimed.
            with store.transaction():
                store.db.execute("UPDATE jobs SET state='pending',owner=NULL,lease_until=NULL WHERE config_id=? AND state='claimed'", (stage_cid,))
            while True:
                if stop_after is not None and batches >= stop_after:
                    raise InterruptedError('Requested checkpoint after saved batches')
                jobs = store.claim(stage_cid, owner, batch_size=5, lease_seconds=180)
                if not jobs:
                    break
                if phase == 'warm':
                    raise ValueError('Warm cache miss; refusing new paid calls')
                call_records = jobs
                if stage_name == 'group':
                    call_records = [dict(job, topic=predictions[job['review_id']]['topic'], intent=predictions[job['review_id']]['intent']) for job in jobs]
                    request = build_group_request(call_records, INITIAL_CATALOG)
                else:
                    request = build_request(call_records, role=stage_name)
                # Conservative UTF-8 byte bound protects context before reserving.
                if len(canonical(request).encode('utf-8')) > 30000:
                    raise ValueError('Pilot request too large for conservative token bound; reduce batch configuration')
                call_id = store.dispatch(run_id, stage_cid, owner, jobs, RESERVATION, phase='resume' if resuming else 'initial')
                with store.transaction():
                    payload = json.loads(store.db.execute('SELECT payload FROM calls WHERE id=?', (call_id,)).fetchone()[0])
                    payload.update(run_phase='cold', request=request, rate_input_per_million=str(INPUT_RATE), rate_output_per_million='0', cached_tokens_reported=False, cached_input_tokens_policy='zero discount; provider does not report cache breakdown')
                    store.db.execute('UPDATE calls SET payload=? WHERE id=?', (canonical(payload), call_id))
                raw = None
                try:
                    if stage_name == 'group':
                        memberships, raw = group_client.evaluate_group(call_records, INITIAL_CATALOG)
                        results = [dict(predictions[r['review_id']], issue_id=r['issue_id']) for r in memberships]
                    else:
                        results, raw = enrich_client.evaluate(call_records, role=stage_name)
                    usage, cost = _usage(raw)
                    if cost is None:
                        store.finish(call_id, error='Missing/unrecognized usage; reconcile billing before proceeding', raw_response=raw, usage=usage)
                        raise RuntimeError('Unknown Jev usage; request saved and spending reservation retained')
                    store.finish(call_id, results, usage=usage, cost=cost, raw_response=raw)
                    batches += 1
                    _export(store, root, dataset, cid, run_id)
                    if cost > RESERVATION:
                        raise RuntimeError('Actual charge exceeded reservation; stop and revise bound')
                except JevError as exc:
                    usage, cost = _usage(exc.raw_response)
                    store.finish(call_id, error=str(exc), usage=usage, cost=cost, retry=False, raw_response=exc.raw_response)
                    raise
            states = store.status(stage_cid)['jobs']
            if any(state != 'completed' and count for state, count in states.items()):
                raise ValueError(stage_name + ' contains incomplete/failed tasks: ' + canonical(states))
            stage_times[stage_name] = time.monotonic() - begin
            stages.append(stage_name)
            active_stage[:] = [None, None]
            return list(store.records(stage_dataset, stage_cid))

        enriched = stage('enrich', dataset, config)
        by_id = {r['review_id']: r for r in enriched if r['status'] == 'completed'}
        selected = [sources[rid] for rid in manifest['verification_ids']]
        verification_dataset = _subset(store, root, 'verify', selected)
        verified = stage('verify', verification_dataset, config_for('verify'))
        _write_lines(root / 'verification.jsonl', verified)
        atomic_json(root / 'verification_comparison.json', [dict(review_id=r['review_id'], disagreements=[k for k in ('topic','intent','severity') if r[k] != by_id[r['review_id']][k]]) for r in verified])
        complaints = [sources[r['review_id']] for r in enriched if r.get('intent') in ('complaint', 'cancellation') and r['status'] == 'completed']
        group_dataset = _subset(store, root, 'group', complaints)
        grouped = stage('group', group_dataset, grouping_config, by_id)
        membership = [dict(issue_id=r['issue_id'], review_id=r['review_id']) for r in grouped]
        with (root / 'membership.csv').open('w', newline='', encoding='utf-8') as handle:
            writer = csv.DictWriter(handle, fieldnames=['issue_id', 'review_id'])
            writer.writeheader(); writer.writerows(membership)
        begin = time.monotonic()
        _export(store, root, dataset, cid, run_id)
        rank(root / 'pilot_records.jsonl', root / 'membership.csv', root / 'ranking.csv')
        stage_times['rank'] = time.monotonic() - begin
        stages.append('rank')
        manifest['jev_stages_complete'] = True
        atomic_json(manifest_path, manifest)
    except Exception as exc:
        error = str(exc)
        raise
    finally:
        if run_id and dataset and cid:
            if active_stage[0]:
                stage_times[active_stage[0]] = time.monotonic() - active_stage[1]
            records, calls = _export(store, root, dataset, cid, run_id)
            previous = json.loads(meta_path.read_text()) if meta_path.exists() else {}
            attempt = dict(elapsed_seconds=time.monotonic()-started, stages_completed=stages, stage_seconds=stage_times, stage_times_seconds=stage_times, error=error)
            attempts = previous.get('attempts', []) + [attempt]
            metadata = dict(phase=phase, run_id=run_id, db_path=str((root / 'pilot.sqlite').resolve()), input_count=100, input_sha256=dataset,
                unique_nonempty_texts=profile['unique_nonempty_texts'], budget_usd=BUDGET, max_workers=1, batch_size=5,
                status_counts=dict(Counter(r['status'] for r in records)), attempts=attempts,
                elapsed_seconds=sum(a['elapsed_seconds'] for a in attempts),
                stages_completed=sorted(set(s for a in attempts for s in a['stages_completed'])),
                stage_seconds={s: sum(a['stage_seconds'].get(s,0) for a in attempts) for s in ('prepare','enrich','verify','group','rank')},
                jev_stages_complete=bool(manifest and manifest.get('jev_stages_complete')), full_pilot_complete=previous.get('full_pilot_complete', False),
                remaining=previous.get('remaining', ['memo']), error=error, ledger=dict(store.db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()))
            if 'memo' in previous.get('stages_completed', []):
                metadata['stages_completed'] = sorted(set(metadata['stages_completed'] + ['memo']))
                metadata['stage_seconds']['memo'] = previous.get('stage_seconds', {}).get('memo', 0)
            metadata['stage_times_seconds'] = metadata['stage_seconds']
            metadata['runtime_basis'] = 'active_attempts'
            metadata['active_elapsed_seconds'] = metadata['elapsed_seconds']
            metadata['end_to_end_wall_seconds'] = None
            metadata['wall_window_seconds'] = (datetime.now(timezone.utc) - datetime.fromisoformat(manifest['created_at'])).total_seconds()
            for key in ('memo', 'memo_model', 'memo_config', 'memo_elapsed_seconds'):
                if key in previous:
                    metadata[key] = previous[key]
            metadata = dict(previous, **metadata)
            atomic_json(meta_path, metadata)
            if isinstance(error, str) and error.startswith('Requested checkpoint'):
                before = root / 'checkpoint_before.json'
                if not before.exists():
                    atomic_json(before, store.snapshot(dataset, cid))
            if manifest.get('jev_stages_complete') and (root / 'checkpoint_before.json').exists():
                atomic_json(root / 'checkpoint_after.json', store.snapshot(dataset, cid))
        store.close()
    return metadata


def load_api_key(env_path='.env'):
    """Load only the needed key, without shell execution or logging secret values."""
    if os.environ.get('TYPESAFE_API_KEY'):
        return
    path = Path(env_path)
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        key, separator, value = line.partition('=')
        if separator and key.strip() in ('TYPESAFE_API_KEY', 'export TYPESAFE_API_KEY'):
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            if value:
                os.environ['TYPESAFE_API_KEY'] = value
            return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', default='data/source/cost_100.csv')
    parser.add_argument('--directory', default='artifacts/pilot')
    parser.add_argument('--phase', choices=['cold','warm'], default='cold')
    parser.add_argument('--stop-after-batches', type=int)
    parser.add_argument('--execute-paid', action='store_true', help='Explicitly allow cold Jev calls within persistent $5 pilot ceiling')
    parser.add_argument('--env-file', default='.env')
    args = parser.parse_args()
    if args.phase == 'cold' and not args.execute_paid:
        parser.error('cold execution requires --execute-paid; no API calls made')
    load_api_key(args.env_file)
    result = run_pilot(args.input, args.directory, args.phase, args.stop_after_batches)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
