"""Offline 100,000-review scope: deterministic sample, exact duplicates and cost model.

No credentials, network requests or provider imports. The sample extends the
instructor's own ordering (manifest seed, lowest SHA-256(seed:review_id) among
unique nonempty valid-rating rows), so golden 50, analysis 10,000,
checkpoint 500 and cost 100 are exact nested prefixes of the 100,000 rows.

Unit costs come from saved pilot calls priced with the editable rates file.
Every number produced here is a projection, not a measurement of a scaled run.
"""
import argparse
import csv
import hashlib
import json
from decimal import Decimal
from pathlib import Path

from .cost import replay
from .storage import FIELDS, atomic_json, file_sha

SEED = 'berkeley-fall-2026-assignment-5-v1'
VERIFY_SEED = 'scale-verify-v1'
VALID_RATINGS = {'1', '2', '3', '4', '5'}
# Measured pilot facts that the call log alone cannot express.
PILOT = dict(unique_texts=100, verified_reviews=10, unique_complaint_texts=52)


def rank_key(review_id, seed=SEED):
    return hashlib.sha256((seed + ':' + review_id).encode()).digest()


def verify_selected(review_id, rate):
    """Declared blind sample: nested across checkpoints, fixed before predictions."""
    value = int.from_bytes(rank_key(review_id, VERIFY_SEED), 'big')
    return value < Decimal(rate) * (1 << 256)


def ranked_rows(full_csv):
    """Instructor-ordered eligible rows; returns (rows, excluded_count)."""
    rows, seen, excluded = [], set(), 0
    with open(full_csv, encoding='utf-8-sig', newline='') as handle:
        for row in csv.DictReader(handle):
            rid = row['review_id']
            if (not rid or rid in seen or not row['review_text'].strip()
                    or row['review_rating'] not in VALID_RATINGS):
                excluded += 1
                continue
            seen.add(rid)
            rows.append({k: row[k] for k in FIELDS})
    rows.sort(key=lambda r: (rank_key(r['review_id']), r['review_id']))
    return rows, excluded


def _ids(path):
    with open(path, encoding='utf-8', newline='') as handle:
        return [row['review_id'] for row in csv.DictReader(handle)]


def duplicate_profile(rows, verify_rate=None):
    """Exact-text counts using the queue's key: the unmodified review_text."""
    texts = [row['review_text'] for row in rows]
    unique = len(set(texts))
    profile = dict(rows=len(rows), unique_texts=unique, duplicate_rows=len(rows) - unique,
                   duplicate_share=round((len(rows) - unique) / len(rows), 6) if rows else 0,
                   mean_text_chars=round(sum(map(len, texts)) / len(texts), 3) if texts else 0)
    if verify_rate is not None:
        chosen = [row for row in rows if verify_selected(row['review_id'], verify_rate)]
        profile.update(verify_rate=str(verify_rate), verified_reviews=len(chosen),
                       verified_unique_texts=len({row['review_text'] for row in chosen}))
    return profile


def write_sample(rows, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)
    return file_sha(path)


def pilot_unit_costs(calls_path, rates_path):
    """Measured cold-pilot cost per unit of work, failed billed attempts included."""
    import tempfile
    with tempfile.TemporaryDirectory() as temp:
        priced = replay(calls_path, rates_path, Path(temp) / 'replay.json')
    if not priced['actual']['api_cost_complete']:
        raise ValueError('Pilot calls contain unknown costs; refusing to build unit costs')
    totals = {}
    for call in priced['calls']:
        if call['run_phase'] == 'cold':
            totals[call['role']] = totals.get(call['role'], Decimal(0)) + Decimal(call['calculated_cost_usd'])
    return dict(
        enrich_per_unique_text=totals['enrich'] / PILOT['unique_texts'],
        verify_per_verified_text=totals['verify'] / PILOT['verified_reviews'],
        group_per_unique_complaint_text=totals['group'] / PILOT['unique_complaint_texts'],
        memo_per_run=totals['memo'],
        pilot_stage_totals={k: str(v) for k, v in sorted(totals.items())},
        complaint_share_of_unique=Decimal(PILOT['unique_complaint_texts']) / PILOT['unique_texts'],
    )


def project(work, units, complaint_share=None, multiplier=Decimal(1), memo_runs=1):
    """Stage costs for given work counts; memo is fixed, never per review."""
    share = Decimal(str(complaint_share)) if complaint_share is not None else units['complaint_share_of_unique']
    multiplier = Decimal(str(multiplier))
    costs = dict(
        enrich=units['enrich_per_unique_text'] * work['unique_texts'] * multiplier,
        verify=units['verify_per_verified_text'] * work['verified_unique_texts'] * multiplier,
        group=units['group_per_unique_complaint_text'] * share * work['unique_texts'] * multiplier,
        memo=units['memo_per_run'] * memo_runs,
    )
    total = sum(costs.values(), Decimal(0))
    return dict(stage_usd={k: f'{v:.6f}' for k, v in costs.items()}, total_usd=f'{total:.6f}',
                complaint_share=str(share), variable_multiplier=str(multiplier))


def build_plan(full_csv, source_dir, output_dir, calls_path, rates_path,
               size=100000, verify_rate='0.10', write_csv=True):
    rows, excluded = ranked_rows(full_csv)
    if len(rows) < size:
        raise ValueError('Not enough eligible rows for the requested sample')
    source = Path(source_dir)
    nesting = {
        'golden_50_to_label.csv': rows[:50], 'cost_100.csv': rows[50:150],
        'checkpoint_500.csv': rows[50:550], 'analysis_10000.csv': rows[50:10050],
    }
    nested = {name: _ids(source / name) == [r['review_id'] for r in subset]
              for name, subset in nesting.items()}
    if not all(nested.values()):
        raise ValueError('Instructor sample order not reproduced: ' + json.dumps(nested))
    sample = rows[:size]
    output = Path(output_dir)
    sample_name = f'sample_{size}.csv'
    sample_sha = write_sample(sample, output / sample_name) if write_csv else None
    units = pilot_unit_costs(calls_path, rates_path)
    scopes = {'checkpoint_500': nesting['checkpoint_500.csv'],
              'analysis_10000': nesting['analysis_10000.csv'], f'sample_{size}': sample}
    profiles = {name: duplicate_profile(subset, verify_rate) for name, subset in scopes.items()}
    # Same database for nested checkpoints: later stages pay only for new texts.
    incremental, previous_texts, previous_verify = {}, set(), set()
    for name, subset in scopes.items():
        texts = {r['review_text'] for r in subset}
        verify = {r['review_text'] for r in subset if verify_selected(r['review_id'], verify_rate)}
        incremental[name] = dict(unique_texts=len(texts - previous_texts),
                                 verified_unique_texts=len(verify - previous_verify))
        previous_texts |= texts
        previous_verify |= verify
    full_nonempty, full_unique = 660609, 484189
    corpus_ratio_unique = round(size * full_unique / full_nonempty)
    projections = {}
    for name, profile in profiles.items():
        scenarios = {
            'base': project(profile, units),
            'conservative_x1.25': project(profile, units, multiplier='1.25'),
            'high_complaint_share_0.70_x1.25': project(profile, units, complaint_share='0.70', multiplier='1.25'),
        }
        scenarios['incremental_base_after_previous_checkpoints'] = project(incremental[name], units)
        projections[name] = scenarios
    plan = dict(
        version='scale-plan-v1', estimated=True,
        full_csv_sha256=file_sha(full_csv), eligible_rows=len(rows), excluded_rows=excluded,
        sample=dict(file=str(output / sample_name), sha256=sample_sha, size=size, seed=SEED,
                    method='First rows by SHA-256(seed:review_id) over unique nonempty valid-rating rows; '
                           'identical ordering to the instructor manifest.',
                    nested_prefixes_verified=nested,
                    includes='golden texts (ranks 1-50, human labels never used), analysis_10000 (51-10,050), ranks 10,051-100,000'),
        verification=dict(seed=VERIFY_SEED, rate=str(verify_rate),
                          method='Review verified if SHA-256(seed:review_id) / 2^256 < rate; declared before any prediction, nested across checkpoints.'),
        duplicate_profiles=profiles, incremental_work=incremental,
        corpus_ratio_comparison=dict(
            unique_texts_if_scaled_from_full_corpus=corpus_ratio_unique,
            unique_texts_measured_in_sample=profiles[f'sample_{size}']['unique_texts'],
            note='Measured sample has fewer duplicates than the full-corpus ratio implies; the sample count is used.'),
        unit_costs={k: (str(v) if isinstance(v, Decimal) else v) for k, v in units.items()},
        unit_cost_source=dict(calls=str(calls_path), rates=str(rates_path),
                              basis='Measured 100-review cold pilot, original classifier, batch 5, one worker; failed billed attempts included.'),
        projections=projections,
        assumptions=[
            'Original classifier and grouping configuration unchanged (adapter hashes are part of cache keys).',
            'Jev bills input tokens only at the dated rates.csv price; output tokens are priced at $0.',
            'Pilot token mix per request (~1,130 input tokens per review, dominated by rubric text) holds; sample texts are slightly shorter than the pilot (no credit taken).',
            'Grouping work scales with unique complaint/cancellation texts; pilot share 52% is refreshed at each checkpoint.',
            'Verification uses unique texts among declared sampled rows; verifier cache is separate from enrichment.',
            'Memo is one fixed call (pilot cost including its failed attempt), never multiplied by review count.',
            'Conservative scenario adds 25% to variable stages for retries/validation failures; it is an assumption, not measured.',
            'Provider billing can differ from rate-calculated cost; local compute cost is unknown.',
        ],
    )
    atomic_json(output / 'scale_plan.json', plan)
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--full', default='data/source/spotify_reviews_18months.csv')
    parser.add_argument('--source-dir', default='data/source')
    parser.add_argument('--output-dir', default='data/scale')
    parser.add_argument('--calls', default='cost/pilot_calls.jsonl')
    parser.add_argument('--rates', default='cost/rates.csv')
    parser.add_argument('--size', type=int, default=100000)
    parser.add_argument('--verify-rate', default='0.10')
    args = parser.parse_args()
    plan = build_plan(args.full, args.source_dir, args.output_dir, args.calls, args.rates,
                      args.size, args.verify_rate)
    print(json.dumps({k: plan[k] for k in ('sample', 'duplicate_profiles', 'incremental_work', 'projections')}, indent=2))


if __name__ == '__main__':
    main()
