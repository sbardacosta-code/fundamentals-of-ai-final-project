"""One bounded OpenAI memo stage sharing the persistent pilot spending ledger."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
import fcntl
import json
import os
from pathlib import Path
import time
import uuid

from .memo import (build_evidence, build_api_request, save_claims, validate_and_render,
                   call_openai, MemoAPIError)
from .storage import Store, atomic_json, canonical, digest, file_sha

MODEL = 'gpt-4.1-mini-2025-04-14'
MAX_OUTPUT_TOKENS = 1500
MAX_REQUEST_BYTES = 128000
RESERVATION = .06
RATES = {'input_per_million': '0.40', 'cached_input_per_million': '0.10',
         'output_per_million': '1.60', 'checked_at': '2026-10-10',
         'source_url': 'https://developers.openai.com/api/docs/models/gpt-4.1-mini'}


def _usage(result):
    usage = result.get('usage')
    if result.get('model') != MODEL or not isinstance(usage, dict):
        return None, None
    inp, out = usage.get('input_tokens'), usage.get('output_tokens')
    details = usage.get('input_tokens_details')
    cached = details.get('cached_tokens') if isinstance(details, dict) else None
    if any(type(value) is not int or value < 0 for value in (inp, out, cached)) or cached > inp:
        return usage, None
    amount = ((inp - cached) * Decimal(RATES['input_per_million'])
              + cached * Decimal(RATES['cached_input_per_million'])
              + out * Decimal(RATES['output_per_million'])) / Decimal(1000000)
    return dict(input_tokens=inp, output_tokens=out, cached_input_tokens=cached), float(amount)


def _write_lines(path, values):
    path = Path(path)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as handle:
            for value in values:
                handle.write(canonical(value) + '\n')
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _export_calls(store, root, run_id):
    values = [json.loads(row[0]) for row in store.db.execute(
        'SELECT payload FROM calls WHERE run_id=? ORDER BY created,id', (run_id,))]
    values = [call for call in values if not call.get('experiment')]
    _write_lines(root / 'pilot_calls.jsonl', values)


def _prepare(store, root, manifest):
    identity = manifest['identity']
    records = list(store.records(identity['dataset_id'], identity['enrichment_config']))
    if any(row['status'] not in ('completed', 'quarantined') for row in records):
        raise ValueError('Enrichment must finish before the memo stage.')
    sources = {row['review_id']: json.loads(row['source_json']) for row in store.db.execute(
        'SELECT r.* FROM reviews r JOIN dataset_rows d USING(review_id) WHERE d.dataset_id=?',
        (identity['dataset_id'],))}
    records = [dict(row, review_text=sources[row['review_id']]['review_text']) for row in records]
    evidence = build_evidence(records, root / 'membership.csv', root / 'ranking.csv')
    body = build_api_request(evidence, model=MODEL, max_output_tokens=MAX_OUTPUT_TOKENS)
    if len(json.dumps(body, ensure_ascii=False).encode('utf-8')) > MAX_REQUEST_BYTES:
        raise ValueError('Memo request exceeds the conservative UTF-8 byte limit.')
    config = {'model': MODEL, 'request': body, 'memo_sha256': file_sha(Path(__file__).with_name('memo.py')),
              'pilot_memo_sha256': file_sha(__file__), 'rates': RATES}
    cache_key = digest(canonical(config))
    atomic_json(root / 'memo_evidence.json', evidence)
    save_claims(evidence, root / 'claims.csv')
    return evidence, config, cache_key


def _reserve(store, run_id, config, cache_key, root, retry_invalid):
    call_id = uuid.uuid4().hex
    with store.transaction():
        ledger = store.db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if not ledger:
            raise ValueError('Pilot run ledger is missing.')
        if ledger['reserved'] > 1e-10:
            raise ValueError('Unresolved in-flight or unknown charges block further paid calls.')
        prior_calls = store.db.execute(
            "SELECT COUNT(*) FROM calls WHERE run_id=? AND json_extract(payload,'$.role')='memo'", (run_id,)).fetchone()[0]
        if prior_calls >= 2:
            raise ValueError('Global memo attempt cap reached: at most two paid attempts across all configurations in this pilot run.')
        previous_invalid = store.db.execute(
            "SELECT 1 FROM pilot_memo_jobs m JOIN calls c ON c.id=m.call_id WHERE c.run_id=? AND m.state='invalid' LIMIT 1",
            (run_id,)).fetchone()
        if previous_invalid and not retry_invalid:
            raise ValueError('A previous memo configuration failed validation; one explicit retry requires --retry-invalid.')
        prior = store.db.execute('SELECT * FROM pilot_memo_jobs WHERE cache_key=?', (cache_key,)).fetchone()
        if prior:
            if prior['state'] != 'invalid' or not retry_invalid or prior['attempt_count'] >= 2:
                raise ValueError('Memo attempt already exists; only one explicit retry of billed invalid output is allowed.')
        if ledger['spent'] + ledger['reserved'] + RESERVATION > ledger['budget']:
            raise ValueError('The shared pilot spending cap would be exceeded.')
        store.db.execute('INSERT OR IGNORE INTO configs VALUES(?,?)', (cache_key, canonical(config)))
        payload = dict(request_id=call_id, run_id=run_id, role='memo', model=MODEL,
                       phase='initial', run_phase='cold', label_config=cache_key,
                       review_ids=[], started_at=datetime.now(timezone.utc).isoformat(),
                       outcome='in_flight', request=config['request'],
                       input_artifact=str(root / 'memo_evidence.json'), rates=RATES,
                       rate_input_per_million=RATES['input_per_million'],
                       rate_output_per_million=RATES['output_per_million'],
                       rate_cached_input_per_million=RATES['cached_input_per_million'])
        store.db.execute('INSERT INTO calls VALUES(?,?,?,?,?,?,?,?)',
                         (call_id, run_id, cache_key, 'memo-' + call_id, RESERVATION,
                          'in_flight', canonical(payload), time.time()))
        store.db.execute('UPDATE runs SET reserved=reserved+? WHERE id=?', (RESERVATION, run_id))
        if prior:
            store.db.execute("UPDATE pilot_memo_jobs SET state='in_flight',call_id=?,attempt_count=attempt_count+1,error=NULL WHERE cache_key=?",
                             (call_id, cache_key))
        else:
            store.db.execute('INSERT INTO pilot_memo_jobs VALUES(?,?,?,?,?,?,?)',
                             (cache_key, 'in_flight', call_id, 1, None, None, None))
    return call_id


def _finish(store, cache_key, call_id, result, rendered, error, state):
    usage, amount = _usage(result) if result else (None, None)
    if amount is None:
        state, rendered = 'uncertain', None
        error = error or 'Unknown usage/model; reconcile charges before proceeding.'
    elif amount > RESERVATION:
        state, rendered = 'failed', None
        error = 'Actual cost exceeded reservation; stop and revise the bound.'
    with store.transaction():
        call = store.db.execute('SELECT * FROM calls WHERE id=?', (call_id,)).fetchone()
        if not call or call['outcome'] != 'in_flight':
            raise ValueError('Memo call is missing or already finalized.')
        payload = json.loads(call['payload'])
        payload.update(outcome='succeeded' if state == 'succeeded' else 'failed',
                       ended_at=datetime.now(timezone.utc).isoformat(), cost_usd=amount,
                       usage_available=usage is not None, error=error,
                       failure_kind='invalid_output' if state == 'invalid' else (state if state != 'succeeded' else None))
        if result:
            payload['raw_response'] = result
            payload['provider_request_id'] = result.get('request_id')
            payload['response_model'] = result.get('model')
            payload['elapsed_seconds'] = result.get('elapsed_seconds')
        if usage:
            payload.update({key: usage[key] for key in ('input_tokens', 'output_tokens', 'cached_input_tokens') if key in usage})
        if amount is not None:
            store.db.execute('UPDATE runs SET reserved=reserved-?,spent=spent+? WHERE id=?',
                             (call['reservation'], amount, call['run_id']))
        store.db.execute('UPDATE calls SET outcome=?,payload=? WHERE id=?',
                         (payload['outcome'], canonical(payload), call_id))
        store.db.execute('UPDATE pilot_memo_jobs SET state=?,result_json=?,error=? WHERE cache_key=?',
                         (state, canonical(rendered) if rendered else None, error, cache_key))
    return state, error


def _metadata(store, root, manifest, phase, elapsed, cache_key, cached, complete, error):
    path = root / (phase + '.json')
    previous = json.loads(path.read_text()) if path.exists() else {}
    attempts = previous.get('memo_attempts', []) + [dict(elapsed_seconds=elapsed, cached=cached,
                                                      completed=complete, error=error)]
    memo_seconds = sum(row['elapsed_seconds'] for row in attempts)
    jev_attempts = previous.get('attempts')
    jev_seconds = (sum(row['elapsed_seconds'] for row in jev_attempts)
                   if isinstance(jev_attempts, list) and jev_attempts
                   else previous.get('jev_elapsed_seconds', previous.get('elapsed_seconds', 0)))
    active_seconds = jev_seconds + memo_seconds
    phase_start = manifest['created_at'] if phase == 'cold' else previous.get('phase_started_at')
    wall_seconds = ((datetime.now(timezone.utc) - datetime.fromisoformat(phase_start)).total_seconds()
                    if phase_start else None)
    result = dict(previous, memo_attempts=attempts, memo_elapsed_seconds=memo_seconds,
                  memo_model=MODEL, memo_config=cache_key, memo_complete=complete,
                  jev_elapsed_seconds=jev_seconds, elapsed_seconds=active_seconds,
                  active_elapsed_seconds=active_seconds, active_execution_seconds=active_seconds,
                  runtime_basis='active_attempts', wall_elapsed_seconds=wall_seconds,
                  pause_seconds=max(0, wall_seconds - active_seconds) if wall_seconds is not None else None,
                  end_to_end_wall_seconds=None,
                  timing_note='Active execution sums nonoverlapping Jev and memo invocations including their overhead. Cold wall window begins at manifest creation and includes pauses. Warm wall duration is unknown unless its own phase_started_at is recorded; pause is the measured wall-minus-active remainder, including between-command overhead.',
                  full_pilot_complete=bool(complete and previous.get('jev_stages_complete')),
                  remaining=[] if complete and previous.get('jev_stages_complete') else ['memo'],
                  ledger=dict(store.db.execute('SELECT * FROM runs WHERE id=?', (manifest['run_id'],)).fetchone()))
    result['stage_seconds'] = dict(previous.get('stage_seconds', {}), memo=memo_seconds)
    result['stage_times_seconds'] = dict(previous.get('stage_times_seconds', previous.get('stage_seconds', {})), memo=memo_seconds)
    if complete:
        result['stages_completed'] = sorted(set(previous.get('stages_completed', []) + ['memo']))
    result['memo'] = dict(cache_hit=cached, complete=complete, error=error,
                          path=str(root / 'memo.json'))
    result['wall_window_seconds'] = wall_seconds
    atomic_json(path, result)
    return result


def run_memo(directory='artifacts/pilot', *, execute_paid=False, phase='cold', retry_invalid=False, client=None):
    """Run or reuse memo under the existing pilot's one persistent budget.

    `phase='warm'` never makes an API call. `execute_paid` must be explicit for a
    cold miss. One validation retry requires another explicit invocation with
    retry_invalid=True. The run allows at most two memo dispatches across every
    configuration, so changing prompts does not reset the allowance. Unknown
    outcomes remain reserved and block duplicates.
    Injectable client is for synthetic tests; production defaults to call_openai.
    """
    if phase not in ('cold', 'warm'):
        raise ValueError('phase must be cold or warm.')
    root = Path(directory)
    if not (root / 'manifest.json').is_file() or not (root / 'pilot.sqlite').is_file():
        raise ValueError('Run the Jev pilot first; memo cannot create a new spending cap.')
    with (root / 'pilot.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another pilot process owns this directory.') from None
        return _run_locked(root, execute_paid, phase, retry_invalid, client or call_openai)


def _run_locked(root, execute_paid, phase, retry_invalid, client):
    started = time.monotonic()
    manifest = json.loads((root / 'manifest.json').read_text())
    if not manifest.get('jev_stages_complete'):
        raise ValueError('Jev stages must complete before generating the memo.')
    if not (root / (phase + '.json')).is_file():
        raise ValueError('Run the matching Jev cold/warm phase before its memo phase.')
    store = Store(root / 'pilot.sqlite')
    cached = complete = False
    cache_key = error = None
    try:
        store.db.execute('''CREATE TABLE IF NOT EXISTS pilot_memo_jobs(
            cache_key TEXT PRIMARY KEY,state TEXT NOT NULL,call_id TEXT NOT NULL,
            attempt_count INTEGER NOT NULL,raw_json TEXT,result_json TEXT,error TEXT)''')
        evidence, config, cache_key = _prepare(store, root, manifest)
        prior = store.db.execute('SELECT * FROM pilot_memo_jobs WHERE cache_key=?', (cache_key,)).fetchone()
        if prior and prior['state'] == 'succeeded':
            rendered = json.loads(prior['result_json'])
            cached = True
        else:
            if phase == 'warm':
                raise ValueError('Warm memo cache miss; refusing paid execution.')
            if not execute_paid:
                raise ValueError('Cold memo cache miss. Use --execute-paid only after authorization.')
            if client is call_openai and not os.environ.get('OPENAI_API_KEY', '').strip():
                raise ValueError('OPENAI_API_KEY is required before reserving a paid memo request.')
            ledger = store.db.execute('SELECT * FROM runs WHERE id=?', (manifest['run_id'],)).fetchone()
            if not ledger or ledger['budget'] != manifest['budget_usd'] or ledger['budget'] > 5:
                raise ValueError('Persistent pilot spending cap is missing or altered.')
            call_id = _reserve(store, manifest['run_id'], config, cache_key, root, retry_invalid)
            result = None
            try:
                result = client(evidence, model=MODEL, allow_paid=True,
                                max_output_tokens=MAX_OUTPUT_TOKENS, timeout=60)
            except Exception as exc:
                message = ('Memo provider HTTP ' + str(exc.status) if isinstance(exc, MemoAPIError) and exc.status else
                           'Memo provider failed; billing outcome requires reconciliation.')
                _finish(store, cache_key, call_id, None, None, message, 'uncertain')
                raise RuntimeError(message) from None
            # Persist raw usage/output before semantic or syntax validation. A crash
            # after this point remains blocked rather than silently calling again.
            raw_path = root / 'memo_raw' / (call_id + '.json')
            atomic_json(raw_path, result)
            with store.transaction():
                store.db.execute('UPDATE pilot_memo_jobs SET raw_json=? WHERE cache_key=?', (canonical(result), cache_key))
            rendered, validation_error, state = None, None, 'succeeded'
            try:
                if result.get('outcome') != 'completed':
                    raise ValueError('Provider did not return a completed memo.')
                rendered = validate_and_render(result.get('text'), evidence)
            except (ValueError, TypeError, KeyError) as exc:
                validation_error, state = str(exc), 'invalid'
            state, validation_error = _finish(store, cache_key, call_id, result, rendered, validation_error, state)
            if state != 'succeeded':
                raise RuntimeError(validation_error or 'Memo failed validation.')
        atomic_json(root / 'memo.json', rendered)
        complete = True
    except Exception as exc:
        error = str(exc)
        raise
    finally:
        try:
            _export_calls(store, root, manifest['run_id'])
            metadata = _metadata(store, root, manifest, phase, time.monotonic() - started,
                                 cache_key, cached, complete, error)
        finally:
            store.close()
    return metadata


def load_api_key(env_path='.env'):
    """Load only OPENAI_API_KEY as literal text; never execute or expand a file."""
    if os.environ.get('OPENAI_API_KEY'):
        return
    path = Path(env_path)
    if not path.is_file():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        name, separator, value = line.partition('=')
        if separator and name.strip() in ('OPENAI_API_KEY', 'export OPENAI_API_KEY'):
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            if value:
                os.environ['OPENAI_API_KEY'] = value
            return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', nargs='?', default='artifacts/pilot')
    parser.add_argument('--execute-paid', action='store_true')
    parser.add_argument('--phase', choices=['cold', 'warm'], default='cold')
    parser.add_argument('--retry-invalid', action='store_true')
    parser.add_argument('--env-file', default='.env', help='Read only OPENAI_API_KEY without shell execution')
    args = parser.parse_args()
    load_api_key(args.env_file)
    result = run_memo(args.directory, execute_paid=args.execute_paid, phase=args.phase,
                      retry_invalid=args.retry_invalid)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
