"""Checkpoint runner for 500 / 10,000 / 100,000 reviews with bounded parallel Jev workers.

Imports and --help make no provider calls. Cold execution requires --execute-paid
and a per-checkpoint hard cap fixed when the checkpoint ledger is created; a
resume can never raise or reset it. Nested checkpoints share one SQLite store,
so exact-text cache hits from earlier checkpoints cost nothing.

Unchanged from the pilot: jev.py, grouping.py and the original classifier
configuration (their file hashes are part of every cache key). New here:
N worker threads with their own SQLite connections, a shared request-rate
limiter with backoff/jitter on HTTP 429, and run-wide stop conditions.
Memo generation is not part of this runner; it remains a separate bounded stage.
"""
import argparse
import csv
import json
import random
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .cli import config_for
from .grouping import (INITIAL_CATALOG, CATALOG_VERSION, GROUP_PROMPT_VERSION,
                       JevGroupingClient, build_group_request)
from .jev import JevClient, JevError, JevHTTPError, JevValidationError, build_request
from .pilot import load_api_key
from .ranking import rank
from .scale_plan import verify_selected
from .storage import Store, FIELDS, atomic_json, canonical, digest, file_sha

INPUT_RATE = Decimal('0.042')  # USD per million Jev input tokens; outputs $0 (cost/rates.csv).
RESERVATION = .01              # Per request; observed batch-5 requests cost ~$0.00024.
MAX_REQUEST_BYTES = 30000
MAX_WORKERS = 8


class StopRun(Exception):
    """A run-wide stop condition; saved work stays saved and resumable."""


def _usage(raw):
    usage = raw.get('usage') if isinstance(raw, dict) else None
    if not isinstance(usage, dict):
        return None, None
    if any(type(usage.get(k)) is not int or usage[k] < 0 for k in ('input_tokens', 'output_tokens')):
        return usage, None
    return dict(usage, cached_input_tokens=0), float(Decimal(usage['input_tokens']) * INPUT_RATE / Decimal(1000000))


class RateLimiter:
    """Shared across workers: minimum spacing between dispatches plus 429 cooldown."""

    def __init__(self, requests_per_second, sleep=time.sleep, clock=time.monotonic):
        self.interval = 1.0 / requests_per_second
        self.lock = threading.Lock()
        self.next_at = 0.0
        self.sleep, self.clock = sleep, clock

    def wait(self):
        with self.lock:
            now = self.clock()
            start = max(now, self.next_at)
            self.next_at = start + self.interval
        if start > now:
            self.sleep(start - now)

    def cooldown(self, seconds):
        with self.lock:
            self.next_at = max(self.next_at, self.clock() + seconds)


def backoff_seconds(attempt, retry_after=None, base=1.0, cap=30.0):
    if retry_after is not None:
        try:
            return min(cap, max(0.0, float(retry_after)))
        except (TypeError, ValueError):
            pass
    return min(cap, base * 2 ** attempt) * (0.5 + random.random() / 2)


class Limits:
    def __init__(self, max_invalid_fraction=0.02, min_invalid_to_stop=3,
                 max_rate_limited=20, max_quarantine_fraction=0.01):
        self.max_invalid_fraction = max_invalid_fraction
        self.min_invalid_to_stop = min_invalid_to_stop
        self.max_rate_limited = max_rate_limited
        self.max_quarantine_fraction = max_quarantine_fraction


def _write_lines(path, rows):
    path = Path(path)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        for row in rows:
            handle.write(canonical(row) + '\n')
    temporary.replace(path)


def _subset(store, folder, name, sources):
    path = folder / (name + '_input.csv')
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows([{k: row[k] for k in FIELDS} for row in sources])
    return store.ingest(path)['file_sha256']


class _Stage:
    """One queue stage drained by N workers sharing a ledger, limiter and stop flag."""

    def __init__(self, db_path, run_id, cid, name, client, workers, batch_size, phase,
                 limiter, limits, stop_after_batches, counters, predictions=None):
        self.db_path, self.run_id, self.cid, self.name = db_path, run_id, cid, name
        self.client, self.workers, self.batch_size, self.phase = client, workers, batch_size, phase
        self.limiter, self.limits, self.stop_after = limiter, limits, stop_after_batches
        self.predictions = predictions
        self.counters = counters
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.reason = None

    def halt(self, reason):
        with self.lock:
            if self.reason is None:
                self.reason = reason
        self.stop.set()

    def _count(self, key):
        with self.lock:
            self.counters[key] += 1
            return self.counters[key]

    def _call(self, records):
        if self.name == 'group':
            memberships, raw = self.client.evaluate_group(records, INITIAL_CATALOG)
            return [dict(self.predictions[r['review_id']], issue_id=r['issue_id']) for r in memberships], raw
        return self.client.evaluate(records, role=self.name)

    def _worker(self, index):
        store = Store(self.db_path)
        owner = f'{self.name}-w{index}-{uuid.uuid4().hex[:8]}'
        try:
            while not self.stop.is_set():
                with self.lock:
                    if self.stop_after is not None and self.counters['batches'] >= self.stop_after:
                        self.halt('Requested checkpoint after saved batches')
                        break
                jobs = store.claim(self.cid, owner, batch_size=self.batch_size, lease_seconds=300)
                if not jobs:
                    break
                records = jobs
                if self.name == 'group':
                    records = [dict(job, topic=self.predictions[job['review_id']]['topic'],
                                    intent=self.predictions[job['review_id']]['intent']) for job in jobs]
                    request = build_group_request(records, INITIAL_CATALOG)
                else:
                    request = build_request(records, role=self.name)
                if len(canonical(request).encode('utf-8')) > MAX_REQUEST_BYTES:
                    self._release(store, jobs)
                    self.halt('Request exceeds conservative byte bound; reduce batch size')
                    break
                self.limiter.wait()
                if self.stop.is_set():
                    self._release(store, jobs)
                    break
                try:
                    call_id = store.dispatch(self.run_id, self.cid, owner, jobs, RESERVATION, phase=self.phase)
                except ValueError as exc:
                    self._release(store, jobs)
                    self.halt(str(exc))
                    break
                with store.transaction():
                    payload = json.loads(store.db.execute('SELECT payload FROM calls WHERE id=?', (call_id,)).fetchone()[0])
                    payload.update(run_phase='cold', request=request, worker=owner,
                                   rate_input_per_million=str(INPUT_RATE), rate_output_per_million='0',
                                   cached_tokens_reported=False,
                                   cached_input_tokens_policy='zero discount; provider does not report cache breakdown')
                    store.db.execute('UPDATE calls SET payload=? WHERE id=?', (canonical(payload), call_id))
                self._execute(store, call_id, records)
        except Exception as exc:  # A worker crash stops the run; queue state remains durable.
            self.halt(f'{type(exc).__name__}: {exc}')
        finally:
            store.close()

    def _release(self, store, jobs):
        with store.transaction():
            for job in jobs:
                store.db.execute("UPDATE jobs SET state='pending',owner=NULL,lease_until=NULL WHERE config_id=? AND text_hash=? AND state='claimed'",
                                 (self.cid, job['text_hash']))

    def _execute(self, store, call_id, records):
        try:
            results, raw = self._call(records)
        except JevHTTPError as exc:
            if exc.outcome_unknown:
                store.finish(call_id, error=str(exc), cost=None, raw_response=exc.raw_response)
                self.halt('Provider HTTP ' + str(exc.status) + ' with unknown outcome; reservation held for reconciliation')
                return
            # Rejected before processing (429 or other 4xx): recorded as unbilled and
            # returned to the queue. A rate limit is not a classification attempt.
            store.finish(call_id, error=str(exc), cost=0.0, retry=True)
            if exc.status != 429:
                self.halt('Provider rejected request with HTTP ' + str(exc.status))
                return
            with store.transaction():
                store.db.execute("UPDATE jobs SET attempt_count=attempt_count-1 WHERE config_id=? AND state='pending' AND attempt_count>0 AND source_id IN (%s)"
                                 % ','.join('?' * len(records)), [self.cid] + [r['review_id'] for r in records])
            limited = self._count('rate_limited')
            self.limiter.cooldown(backoff_seconds(min(limited, 5), exc.retry_after))
            if limited > self.limits.max_rate_limited:
                self.halt('Too many HTTP 429 responses; lower workers or rate')
            return
        except JevValidationError as exc:
            usage, cost = _usage(exc.raw_response)
            # One invalid-output retry per task (the store quarantines a second failure).
            store.finish(call_id, error=str(exc), usage=usage, cost=cost, retry=cost is not None,
                         raw_response=exc.raw_response)
            invalid = self._count('invalid')
            if cost is None:
                self.halt('Invalid output without usable billing; reconcile before resuming')
            elif invalid >= self.limits.min_invalid_to_stop and invalid > self.limits.max_invalid_fraction * max(1, self.counters['requests']):
                self.halt('Invalid-output rate exceeded the declared limit')
            return
        except JevError as exc:
            usage, cost = _usage(exc.raw_response)
            store.finish(call_id, error=str(exc), usage=usage, cost=None, raw_response=exc.raw_response)
            self.halt('Unknown provider outcome; reservation held for reconciliation')
            return
        usage, cost = _usage(raw)
        self._count('requests')
        if cost is None:
            store.finish(call_id, error='Missing/unrecognized usage; reconcile billing', raw_response=raw, usage=usage)
            self.halt('Unknown Jev usage; reservation held')
            return
        try:
            store.finish(call_id, results, usage=usage, cost=cost, raw_response=raw)
        except ValueError as exc:  # Contract validation (IDs, quotes, labels) failed after billing.
            store.finish(call_id, error=str(exc), usage=usage, cost=cost, retry=True, raw_response=raw)
            if self._count('invalid') >= self.limits.min_invalid_to_stop:
                self.halt('Repeated contract-validation failures')
            return
        self._count('batches')
        if cost > RESERVATION:
            self.halt('Actual charge exceeded per-request reservation')

    def run(self):
        threads = [threading.Thread(target=self._worker, args=(i,), daemon=True) for i in range(self.workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return self.reason


def _ledger(store, run_id):
    return dict(store.db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone())


def run_checkpoint(input_path, name, *, root='artifacts/scale', budget_usd, workers=1,
                   batch_size=5, verify_rate='0.10', requests_per_second=4.0,
                   execute_paid=False, stop_after_batches=None, limits=None,
                   enrich_client=None, group_client=None, expected_rows=None):
    """Run or resume one named checkpoint. Returns saved metadata."""
    if not execute_paid:
        raise ValueError('Paid checkpoint requires execute_paid=True after explicit approval')
    if type(workers) is not int or not 1 <= workers <= MAX_WORKERS:
        raise ValueError(f'workers must be 1..{MAX_WORKERS}')
    if type(batch_size) is not int or not 1 <= batch_size <= 50:
        raise ValueError('batch_size must be 1..50')
    if not isinstance(budget_usd, (int, float)) or not 0 < budget_usd <= 10:
        raise ValueError('A hard cap between $0 and $10 is required')
    limits = limits or Limits()
    root = Path(root)
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    started_wall = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    store = Store(root / 'scale.sqlite')
    manifest_path = folder / 'manifest.json'
    stage_seconds, stages, error, run_id = {}, [], None, None
    counters = Counter(batches=0, requests=0, invalid=0, rate_limited=0)
    dataset = cid = None
    try:
        profile = store.ingest(input_path)
        if expected_rows is not None and profile['counts']['records'] != expected_rows:
            raise ValueError(f'Expected {expected_rows} rows, found {profile["counts"]["records"]}')
        dataset = profile['file_sha256']
        config = config_for()
        cid = store.prepare(dataset, config)
        verify_config = config_for('verify')
        grouping_config = dict(role='group', model=config['model'], prompt_version=GROUP_PROMPT_VERSION,
                               catalog=INITIAL_CATALOG, catalog_version=CATALOG_VERSION,
                               adapter_sha256=file_sha(Path(__file__).with_name('grouping.py')),
                               enrichment_config=cid)
        identity = dict(dataset_id=dataset, enrichment_config=cid,
                        verification_config=digest(canonical(verify_config)),
                        grouping_config=digest(canonical(grouping_config)),
                        batch_size=batch_size, verify_rate=str(verify_rate))
        sources = {row['review_id']: json.loads(row['source_json']) for row in store.db.execute(
            'SELECT r.* FROM reviews r JOIN dataset_rows d USING(review_id) WHERE d.dataset_id=? ORDER BY d.ordinal', (dataset,))}
        resuming = manifest_path.exists()
        if resuming:
            manifest = json.loads(manifest_path.read_text())
            if manifest['identity'] != identity:
                raise ValueError('Checkpoint input/config changed; create a new named checkpoint instead')
            if manifest['budget_usd'] != budget_usd:
                raise ValueError('The hard cap is fixed at creation; it cannot be changed on resume')
            run_id = manifest['run_id']
        else:
            run_id = store.create_run(budget_usd)
            verification_ids = [rid for rid, row in sources.items()
                                if row['review_text'].strip() and verify_selected(rid, verify_rate)]
            manifest = dict(identity=identity, run_id=run_id, budget_usd=budget_usd, name=name,
                            input=str(input_path), input_rows=profile['counts']['records'],
                            unique_nonempty_texts=profile['unique_nonempty_texts'],
                            verification_ids=verification_ids, created_at=started_wall,
                            stages_complete=False)
            atomic_json(manifest_path, manifest)
        ledger = _ledger(store, run_id)
        if ledger['budget'] != budget_usd:
            raise ValueError('Persistent checkpoint ledger altered')
        if ledger['reserved'] > 1e-10:
            raise ValueError('Unresolved in-flight/unknown charges require reconciliation before more calls')
        stage_seconds['prepare'] = time.monotonic() - started
        stages.append('prepare')
        limiter = RateLimiter(requests_per_second)
        enrich_client = enrich_client or JevClient()
        group_client = group_client or JevGroupingClient()
        phase = 'resume' if resuming else 'initial'

        def stage(stage_name, stage_dataset, stage_config, client, predictions=None):
            begin = time.monotonic()
            stage_cid = store.prepare(stage_dataset, stage_config)
            with store.transaction():  # Undispatched claims from a crashed process.
                store.db.execute("UPDATE jobs SET state='pending',owner=NULL,lease_until=NULL WHERE config_id=? AND state='claimed'", (stage_cid,))
            runner = _Stage(root / 'scale.sqlite', run_id, stage_cid, stage_name, client, workers, batch_size,
                            phase, limiter, limits, stop_after_batches, counters, predictions)
            reason = runner.run()
            stage_seconds[stage_name] = stage_seconds.get(stage_name, 0) + time.monotonic() - begin
            if reason:
                raise StopRun(f'{stage_name}: {reason}')
            states = store.status(stage_cid)['jobs']
            quarantined = states.get('quarantined', 0)
            total = sum(states.values())
            if quarantined > limits.max_quarantine_fraction * total:
                raise StopRun(f'{stage_name}: quarantined tasks {quarantined}/{total} exceed limit')
            if any(state not in ('completed', 'quarantined') and count for state, count in states.items()):
                raise StopRun(f'{stage_name}: incomplete tasks ' + canonical(states))
            stages.append(stage_name)
            return list(store.records(stage_dataset, stage_cid))

        enriched = stage('enrich', dataset, config, enrich_client)
        by_id = {r['review_id']: r for r in enriched if r['status'] == 'completed'}
        selected = [sources[rid] for rid in manifest['verification_ids'] if rid in by_id]
        verified = stage('verify', _subset(store, folder, 'verify', selected), verify_config, enrich_client)
        _write_lines(folder / 'verification.jsonl', verified)
        comparison = [dict(review_id=r['review_id'],
                           disagreements=[k for k in ('topic', 'intent', 'severity') if r.get(k) != by_id[r['review_id']][k]])
                      for r in verified if r['status'] == 'completed']
        atomic_json(folder / 'verification_comparison.json', comparison)
        complaints = [sources[r['review_id']] for r in enriched
                      if r['status'] == 'completed' and r.get('intent') in ('complaint', 'cancellation')]
        grouped = stage('group', _subset(store, folder, 'group', complaints), grouping_config, group_client, by_id)
        with (folder / 'membership.csv').open('w', newline='', encoding='utf-8') as handle:
            writer = csv.DictWriter(handle, fieldnames=['issue_id', 'review_id'])
            writer.writeheader()
            writer.writerows([dict(issue_id=r['issue_id'], review_id=r['review_id']) for r in grouped if r['status'] == 'completed'])
        begin = time.monotonic()
        _write_lines(folder / 'records.jsonl', store.records(dataset, cid))
        rank(folder / 'records.jsonl', folder / 'membership.csv', folder / 'ranking.csv')
        stage_seconds['rank'] = time.monotonic() - begin
        stages.append('rank')
        manifest['stages_complete'] = True
        atomic_json(manifest_path, manifest)
    except Exception as exc:
        error = str(exc)
        if not isinstance(exc, StopRun):
            raise
    finally:
        metadata = {}
        if run_id and dataset and cid:
            records = list(store.records(dataset, cid))
            _write_lines(folder / 'records.jsonl', records)
            calls = [json.loads(row[0]) for row in store.db.execute(
                'SELECT payload FROM calls WHERE run_id=? ORDER BY created,id', (run_id,))]
            _write_lines(folder / 'calls.jsonl', calls)
            snapshot = store.snapshot(dataset, cid)
            if error and error.endswith('Requested checkpoint after saved batches') and not (folder / 'checkpoint_before.json').exists():
                atomic_json(folder / 'checkpoint_before.json', snapshot)
            if manifest_path.exists() and json.loads(manifest_path.read_text()).get('stages_complete'):
                atomic_json(folder / 'checkpoint_after.json', snapshot)
            previous = json.loads((folder / 'run.json').read_text()) if (folder / 'run.json').exists() else {}
            elapsed = time.monotonic() - started
            attempt = dict(started_at=started_wall, elapsed_seconds=elapsed, workers=workers,
                           batch_size=batch_size, requests_per_second=requests_per_second,
                           stages_completed=stages, stage_seconds=stage_seconds,
                           counters=dict(counters), error=error)
            ledger = _ledger(store, run_id)
            role_costs = Counter()
            role_calls = Counter()
            for call in calls:
                role_calls[(call['role'], call.get('outcome'))] += 1
                if call.get('cost_usd') is not None:
                    role_costs[call['role']] += Decimal(str(call['cost_usd']))
            metadata = dict(
                name=name, run_id=run_id, input=str(input_path), dataset_id=dataset,
                enrichment_config=cid, budget_usd=budget_usd, ledger=ledger,
                remaining_budget_usd=ledger['budget'] - ledger['spent'] - ledger['reserved'],
                status_counts=dict(Counter(r['status'] for r in records)),
                calls_by_role_outcome={f'{k[0]}:{k[1]}': v for k, v in sorted(role_calls.items())},
                cost_by_role_usd={k: str(v) for k, v in sorted(role_costs.items())},
                cache_reused_rows=sum(1 for r in records if r.get('cache_source_id')),
                stages_complete=bool(manifest_path.exists() and json.loads(manifest_path.read_text()).get('stages_complete')),
                attempts=previous.get('attempts', []) + [attempt], error=error)
            atomic_json(folder / 'run.json', metadata)
        store.close()
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--name', required=True, help='checkpoint folder under --root, e.g. checkpoint_500')
    parser.add_argument('--root', default='artifacts/scale')
    parser.add_argument('--budget-usd', type=float, required=True, help='hard cap fixed at creation')
    parser.add_argument('--workers', type=int, default=1)
    parser.add_argument('--batch-size', type=int, default=5)
    parser.add_argument('--verify-rate', default='0.10')
    parser.add_argument('--requests-per-second', type=float, default=4.0)
    parser.add_argument('--expected-rows', type=int)
    parser.add_argument('--stop-after-batches', type=int, help='interrupt after N saved batches (resume demo)')
    parser.add_argument('--execute-paid', action='store_true', help='explicitly allow paid Jev calls')
    parser.add_argument('--env-file', default='.env')
    args = parser.parse_args()
    if not args.execute_paid:
        parser.error('paid checkpoint requires --execute-paid after explicit approval; no API calls made')
    load_api_key(args.env_file)
    result = run_checkpoint(args.input, args.name, root=args.root, budget_usd=args.budget_usd,
                            workers=args.workers, batch_size=args.batch_size, verify_rate=args.verify_rate,
                            requests_per_second=args.requests_per_second, execute_paid=True,
                            stop_after_batches=args.stop_after_batches, expected_rows=args.expected_rows)
    print(json.dumps({k: result.get(k) for k in ('name', 'status_counts', 'ledger', 'cost_by_role_usd',
                                                 'stages_complete', 'error')}, indent=2))


if __name__ == '__main__':
    main()
