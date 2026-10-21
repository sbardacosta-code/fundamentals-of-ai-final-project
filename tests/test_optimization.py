"""Offline trial safety tests; every provider is a synthetic fake."""
import csv
import json
import tempfile
import unittest
from pathlib import Path

from review_pipeline.jev import JevValidationError
from review_pipeline.optimization import EXPERIMENT, run_trial
from review_pipeline.storage import FIELDS, Store, atomic_json, canonical


class FakeClient:
    def __init__(self, mode='success'):
        self.mode = mode
        self.calls = []

    def evaluate(self, records):
        self.calls.append([r['review_id'] for r in records])
        raw = {'model': 'jev-1.13.0', 'usage': {'input_tokens': 100, 'output_tokens': 5}}
        if self.mode == 'known_failure':
            raise JevValidationError('synthetic invalid output', raw_response=raw)
        if self.mode == 'unknown_usage':
            raw.pop('usage')
        results = [dict(review_id=r['review_id'], topic='other', intent='complaint',
                        severity=2, sentiment=-.5, evidence_quote=r['review_text'], entities=[], needs_review=False) for r in records]
        return results, raw


class OptimizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.pilot = self.root / 'pilot'
        self.pilot.mkdir()
        self.output = self.root / 'trials'
        source = self.root / 'source.csv'
        with source.open('w', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            for i in range(100):
                writer.writerow(dict(zip(FIELDS, [str(i), 'synthetic complaint ' + str(i), '1', '0', '1', '2026-01-01'])))
        self.store = Store(self.pilot / 'pilot.sqlite')
        dataset = self.store.ingest(source)['file_sha256']
        self.dataset = dataset
        self.run_id = self.store.create_run(5)
        atomic_json(self.pilot / 'manifest.json', dict(run_id=self.run_id, identity={'dataset_id': dataset}, budget_usd=5))
        for filename in ('pilot_records.jsonl', 'pilot_calls.jsonl', 'cold.json', 'warm.json'):
            (self.pilot / filename).write_text('original artifact ' + filename)
        self.originals = {p.name: p.read_bytes() for p in self.pilot.iterdir() if p.suffix in ('.json', '.jsonl')}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def trial(self, client, variant='compact'):
        return run_trial(variant, pilot_dir=self.pilot, output=self.output, client=client)

    def seed_spend(self, amount, experiment=None):
        config = {'role': 'enrich', 'model': 'synthetic', 'seed': amount}
        cid = self.store.prepare(self.dataset, config)
        jobs = self.store.claim(cid, 'seed', batch_size=1)
        call = self.store.dispatch(self.run_id, cid, 'seed', jobs, amount)
        if experiment:
            payload = json.loads(self.store.db.execute('SELECT payload FROM calls WHERE id=?', (call,)).fetchone()[0])
            payload.update(experiment=experiment, trial='previous-variant')
            self.store.db.execute('UPDATE calls SET payload=? WHERE id=?', (canonical(payload), call))
        self.store.finish(call, error='synthetic prior charge', cost=amount)

    def test_existing_ledger_and_cache_replay_zero_new_calls_preserve_pilot(self):
        fake = FakeClient()
        result = self.trial(fake)
        self.assertTrue(result['complete'])
        self.assertEqual(result['count'], 100)
        self.assertEqual(len(fake.calls), 20)
        self.assertEqual(result['ledger']['id'], self.run_id)
        self.assertEqual(result['ledger']['budget'], 5)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM runs').fetchone()[0], 1)
        first_spend = result['ledger']['spent']
        cached = self.trial(fake)
        self.assertTrue(cached['complete'])
        self.assertEqual(len(fake.calls), 20)
        self.assertEqual(cached['ledger']['spent'], first_spend)
        for filename, content in self.originals.items():
            self.assertEqual((self.pilot / filename).read_bytes(), content)

    def test_trial_sublimit_includes_previous_variant_charges(self):
        self.seed_spend(.245, EXPERIMENT)
        fake = FakeClient()
        with self.assertRaisesRegex(ValueError, 'sub-budget'):
            self.trial(fake, variant='shared')
        self.assertEqual(fake.calls, [])
        ledger = self.store.status()['runs'][0]
        self.assertAlmostEqual(ledger['spent'], .245)
        self.assertEqual(ledger['reserved'], 0)

    def test_global_five_dollar_cap_includes_non_trial_spending(self):
        self.seed_spend(4.995)
        fake = FakeClient()
        with self.assertRaisesRegex(ValueError, 'Spending cap'):
            self.trial(fake)
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.store.status()['runs'][0]['reserved'], 0)

    def test_unknown_usage_retains_reservation_and_blocks_all_variants(self):
        fake = FakeClient('unknown_usage')
        with self.assertRaisesRegex(RuntimeError, 'Unknown usage'):
            self.trial(fake)
        self.assertEqual(len(fake.calls), 1)
        self.assertAlmostEqual(self.store.status()['runs'][0]['reserved'], .01)
        fresh = FakeClient()
        with self.assertRaisesRegex(ValueError, 'Unresolved reservations'):
            self.trial(fresh, variant='shared')
        self.assertEqual(fresh.calls, [])
        metadata = json.loads((self.output / 'compact-b5-meta.json').read_text())
        self.assertEqual(metadata['unknown_cost_attempts'], 1)
        self.assertFalse(metadata['complete'])

    def test_billed_validation_failure_retries_at_most_twice_per_record(self):
        fake = FakeClient('known_failure')
        result = self.trial(fake)
        self.assertFalse(result['complete'])
        self.assertEqual(len(fake.calls), 40)
        counts = {}
        for batch in fake.calls:
            for rid in batch:
                counts[rid] = counts.get(rid, 0) + 1
        self.assertEqual(set(counts.values()), {2})
        self.assertEqual(len(counts), 100)
        states = self.store.status(result['config_id'])['jobs']
        self.assertEqual(states, {'quarantined': 100})
        again = self.trial(fake)
        self.assertFalse(again['complete'])
        self.assertEqual(len(fake.calls), 40)
        self.assertGreater(result['ledger']['spent'], 0)


if __name__ == '__main__':
    unittest.main()
