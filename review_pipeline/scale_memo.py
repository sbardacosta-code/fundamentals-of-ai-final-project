"""Bounded recommendation memo for a scaled checkpoint, written by Claude Haiku 5.5.

Jev stays the classifier; this is the separate lightweight generative role.
One call per checkpoint evidence pack, billed to the Anthropic API key in
ANTHROPIC_API_KEY (Console API credits, not a Claude subscription). Imports and
--help make no calls. Paid execution needs --execute-paid and a hard cap fixed
when the memo ledger is created. At most two attempts (one invalid-output retry).
Evidence, prompt and validation reuse memo.py unchanged.
"""
import argparse
import json
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .memo import build_evidence, make_prompt, save_claims, validate_and_render
from .storage import Store, atomic_json, canonical, digest, file_sha

MODEL = 'claude-haiku-5-5'
MAX_OUTPUT_TOKENS = 4000  # Adaptive thinking is on by default and counts as output.
RESERVATION = .01         # Worst case: ~6k input + 4k output tokens is about $0.0026.
RATES = {'input_per_million': '0.10', 'output_per_million': '0.50', 'cached_input_per_million': '0.01',
         'checked_at': '2026-10-20', 'source_url': 'https://platform.claude.com/docs/en/about-claude/pricing'}
FIELDS = ('recommendation', 'alternatives', 'limitations', 'claims', 'issue_ids', 'review_ids')


def build_request(evidence):
    instructions, input_text = make_prompt(evidence)
    properties = {'recommendation': {'type': 'string'}}
    for field in FIELDS[1:]:
        properties[field] = {'type': 'array', 'items': {'type': 'string'}}
    schema = {'type': 'object', 'properties': properties, 'required': list(FIELDS), 'additionalProperties': False}
    return dict(model=MODEL, max_tokens=MAX_OUTPUT_TOKENS, system=instructions,
                messages=[{'role': 'user', 'content': input_text}],
                output_config={'effort': 'low', 'format': {'type': 'json_schema', 'schema': schema}})


def call_haiku(request):
    """One paid Messages request; SDK retries disabled so every attempt is logged."""
    import anthropic
    key = os.environ.get('ANTHROPIC_API_KEY', '').strip()
    if not key:
        raise ValueError('ANTHROPIC_API_KEY is required for the Haiku memo')
    client = anthropic.Anthropic(api_key=key, max_retries=0, timeout=120)
    response = client.messages.create(**request)
    text = next((b.text for b in response.content if b.type == 'text'), None)
    usage = response.usage
    return dict(provider='anthropic', request_id=response._request_id, model=response.model,
                stop_reason=response.stop_reason, text=text,
                usage=dict(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                           cache_read_input_tokens=usage.cache_read_input_tokens or 0,
                           cache_creation_input_tokens=usage.cache_creation_input_tokens or 0))


def price(usage):
    if not isinstance(usage, dict) or any(type(usage.get(k)) is not int or usage[k] < 0
                                          for k in ('input_tokens', 'output_tokens')):
        return None
    cached = usage.get('cache_read_input_tokens') or 0
    created = usage.get('cache_creation_input_tokens') or 0
    if created:
        return None  # No cache writes are requested; an unexpected write is unpriced here.
    return float((Decimal(usage['input_tokens']) * Decimal(RATES['input_per_million'])
                  + Decimal(cached) * Decimal(RATES['cached_input_per_million'])
                  + Decimal(usage['output_tokens']) * Decimal(RATES['output_per_million'])) / Decimal(1000000))


def run_memo(folder, *, root='artifacts/scale', budget_usd=0.10, execute_paid=False, client=call_haiku):
    folder = Path(root) / folder if not Path(folder).is_dir() else Path(folder)
    manifest = json.loads((folder / 'manifest.json').read_text())
    if not manifest.get('stages_complete'):
        raise ValueError('Jev stages must complete before the memo')
    store = Store(Path(root) / 'scale.sqlite')
    try:
        store.db.execute('''CREATE TABLE IF NOT EXISTS scale_memo_jobs(cache_key TEXT PRIMARY KEY, folder TEXT NOT NULL,
            run_id TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL, result_json TEXT, error TEXT)''')
        sources = {r['review_id']: json.loads(r['source_json'])['review_text'] for r in store.db.execute(
            'SELECT r.* FROM reviews r JOIN dataset_rows d USING(review_id) WHERE d.dataset_id=?',
            (manifest['identity']['dataset_id'],))}
        records = [dict(r, review_text=sources[r['review_id']])
                   for r in map(json.loads, (folder / 'records.jsonl').read_text().splitlines())]
        evidence = build_evidence(records, folder / 'membership.csv', folder / 'ranking.csv')
        request = build_request(evidence)
        config = dict(request=request, rates=RATES, memo_sha256=file_sha(Path(__file__).with_name('memo.py')),
                      scale_memo_sha256=file_sha(__file__))
        cache_key = digest(canonical(config))
        atomic_json(folder / 'memo_evidence.json', evidence)
        save_claims(evidence, folder / 'claims.csv')
        job = store.db.execute('SELECT * FROM scale_memo_jobs WHERE cache_key=?', (cache_key,)).fetchone()
        if job and job['state'] == 'succeeded':
            rendered = json.loads(job['result_json'])
            atomic_json(folder / 'memo.json', rendered)
            return dict(cached=True, memo=rendered, cache_key=cache_key)
        if not execute_paid:
            raise ValueError('Memo cache miss; paid execution needs --execute-paid after approval')
        if job and (job['state'] != 'invalid' or job['attempts'] >= 2):
            raise ValueError('Memo attempt exists with state ' + job['state'] + '; reconcile instead of retrying')
        run_id = job['run_id'] if job else store.create_run(budget_usd)
        call_id = uuid.uuid4().hex
        with store.transaction():
            ledger = store.db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if ledger['budget'] != budget_usd:
                raise ValueError('Memo cap is fixed at creation')
            if ledger['reserved'] > 1e-10 or ledger['spent'] + RESERVATION > ledger['budget']:
                raise ValueError('Memo spending cap or unresolved reservation blocks the call')
            store.db.execute('INSERT OR IGNORE INTO configs VALUES(?,?)', (cache_key, canonical(config)))
            payload = dict(request_id=call_id, run_id=run_id, role='memo', model=MODEL, phase='initial',
                           run_phase='cold', label_config=cache_key, review_ids=[], outcome='in_flight',
                           started_at=datetime.now(timezone.utc).isoformat(), request=request,
                           input_artifact=str(folder / 'memo_evidence.json'), rates=RATES)
            store.db.execute('INSERT INTO calls VALUES(?,?,?,?,?,?,?,?)', (call_id, run_id, cache_key, 'memo-' + call_id,
                             RESERVATION, 'in_flight', canonical(payload), time.time()))
            store.db.execute('UPDATE runs SET reserved=reserved+? WHERE id=?', (RESERVATION, run_id))
            if job:
                store.db.execute("UPDATE scale_memo_jobs SET state='in_flight',attempts=attempts+1 WHERE cache_key=?", (cache_key,))
            else:
                store.db.execute("INSERT INTO scale_memo_jobs VALUES(?,?,?,'in_flight',1,NULL,NULL)", (cache_key, str(folder), run_id))
        started = time.monotonic()
        result, error, state, rendered, cost = None, None, 'uncertain', None, None
        try:
            result = client(request)
            atomic_json(folder / 'memo_raw' / (call_id + '.json'), result)
            cost = price(result.get('usage'))
            if cost is None:
                error = 'Unknown usage; reconcile charges'
            elif result.get('model') != MODEL or result.get('stop_reason') != 'end_turn':
                state, error = 'invalid', f"Unexpected model/stop_reason: {result.get('model')}/{result.get('stop_reason')}"
            else:
                try:
                    rendered = validate_and_render(result.get('text'), evidence)
                    state = 'succeeded'
                except (ValueError, TypeError, KeyError) as exc:
                    state, error = 'invalid', str(exc)
        except Exception as exc:
            error = f'{type(exc).__name__}: memo call failed; billing outcome requires reconciliation'
        with store.transaction():
            log = json.loads(store.db.execute('SELECT payload FROM calls WHERE id=?', (call_id,)).fetchone()[0])
            log.update(outcome='succeeded' if state == 'succeeded' else 'failed', cost_usd=cost, error=error,
                       ended_at=datetime.now(timezone.utc).isoformat(), elapsed_seconds=time.monotonic() - started)
            if result:
                log.update(raw_response=result, provider_request_id=result.get('request_id'),
                           response_model=result.get('model'), **(result.get('usage') or {}))
            if cost is not None:
                store.db.execute('UPDATE runs SET reserved=reserved-?,spent=spent+? WHERE id=?', (RESERVATION, cost, run_id))
            store.db.execute('UPDATE calls SET outcome=?,payload=? WHERE id=?', (log['outcome'], canonical(log), call_id))
            store.db.execute('UPDATE scale_memo_jobs SET state=?,result_json=?,error=? WHERE cache_key=?',
                             (state, canonical(rendered) if rendered else None, error, cache_key))
        calls = [json.loads(r[0]) for r in store.db.execute('SELECT payload FROM calls WHERE run_id=? ORDER BY created', (run_id,))]
        (folder / 'memo_calls.jsonl').write_text(''.join(canonical(c) + '\n' for c in calls))
        ledger = dict(store.db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone())
        meta = dict(state=state, error=error, cost_usd=cost, ledger=ledger, model=MODEL, cache_key=cache_key)
        atomic_json(folder / 'memo_run.json', meta)
        if state != 'succeeded':
            raise RuntimeError(error)
        atomic_json(folder / 'memo.json', rendered)
        return dict(cached=False, memo=rendered, **meta)
    finally:
        store.close()


def load_api_key(env_path='.env'):
    """Load only ANTHROPIC_API_KEY as literal text; never print it."""
    if os.environ.get('ANTHROPIC_API_KEY'):
        return
    path = Path(env_path)
    if not path.is_file():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        name, separator, value = line.partition('=')
        if separator and name.strip() in ('ANTHROPIC_API_KEY', 'export ANTHROPIC_API_KEY'):
            value = value.strip().strip('"').strip("'")
            if value:
                os.environ['ANTHROPIC_API_KEY'] = value
            return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', help='checkpoint folder name under --root, e.g. checkpoint_500')
    parser.add_argument('--root', default='artifacts/scale')
    parser.add_argument('--budget-usd', type=float, default=0.10)
    parser.add_argument('--execute-paid', action='store_true')
    parser.add_argument('--env-file', default='.env')
    args = parser.parse_args()
    if args.execute_paid:
        load_api_key(args.env_file)
    result = run_memo(args.folder, root=args.root, budget_usd=args.budget_usd, execute_paid=args.execute_paid)
    print(json.dumps({k: result.get(k) for k in ('cached', 'state', 'cost_usd', 'ledger', 'error')}, indent=2))


if __name__ == '__main__':
    main()
