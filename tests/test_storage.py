"""Synthetic-only tests for durable queue, cache and accounting invariants."""
import csv
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from review_pipeline.storage import FIELDS, Store


def prediction(record):
    return dict(review_id=record['review_id'], topic='other', intent='complaint',
                severity=2, sentiment=-.5, entities=[],
                evidence_quote=record['review_text'], needs_review=False)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dbpath = self.root / 'queue.sqlite'
        self.store = Store(self.dbpath)
        self.config = dict(role='enrich', model='jev-1.13.0', prompt='v1', schema='v1')

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def ingest(self, pairs, name='input.csv'):
        path = self.root / name
        with path.open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            for rid, text in pairs:
                writer.writerow(dict(review_id=rid, review_text=text, review_rating='1',
                                     review_likes='0', app_version='1.0', review_timestamp='2026-01-01'))
        profile = self.store.ingest(path)
        return profile['file_sha256'], profile

    def setup_jobs(self, pairs):
        did, _ = self.ingest(pairs)
        cid = self.store.prepare(did, self.config)
        return did, cid

    def test_independent_connections_concurrent_claims_are_unique(self):
        _, cid = self.setup_jobs([(str(i), 'synthetic text ' + str(i)) for i in range(24)])
        barrier = Barrier(4)
        def work(index):
            store = Store(self.dbpath)
            try:
                barrier.wait(timeout=10)
                return store.claim(cid, 'worker-' + str(index), batch_size=6)
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=4) as workers:
            batches = list(workers.map(work, range(4)))
        ids = [r['review_id'] for batch in batches for r in batch]
        self.assertEqual(len(ids), 24)
        self.assertEqual(len(set(ids)), 24)
        self.assertEqual(self.store.status(cid)['jobs'], {'claimed': 24})

    def test_exact_duplicates_have_direct_original_provenance_and_source_hashes(self):
        did, cid = self.setup_jobs([('first', 'Same exact text'), ('alias', 'Same exact text'),
                                   ('third', 'Same exact text'), ('empty', '  ')])
        batch = self.store.claim(cid, 'worker')
        self.assertEqual([r['review_id'] for r in batch], ['first'])
        run = self.store.create_run(1)
        call = self.store.dispatch(run, cid, 'worker', batch, .1)
        self.store.finish(call, [prediction(batch[0])], usage={'input_tokens': 10}, cost=.01)
        records = {r['review_id']: r for r in self.store.records(did, cid)}
        self.assertNotIn('cache_source_id', records['first'])
        self.assertEqual(records['alias']['cache_source_id'], 'first')
        self.assertEqual(records['third']['cache_source_id'], 'first')
        self.assertNotEqual(records['first']['source_sha256'], records['alias']['source_sha256'])
        self.assertEqual(records['empty']['reason'], 'empty_review_text')
        self.assertEqual(self.store.claim(cid, 'warm'), [])
        self.assertEqual(self.store.snapshot(did, cid)['completed_ids'], ['alias', 'first', 'third'])

    def test_duplicate_ids_roll_back_whole_import_without_harming_prior_dataset(self):
        did, _ = self.ingest([('existing', 'preserve me')], name='good.csv')
        for pairs in ([('new', 'new'), ('new', 'new')], [('new', 'new'), ('existing', 'changed')]):
            path = self.root / 'bad.csv'
            with path.open('w', newline='', encoding='utf-8') as stream:
                writer = csv.DictWriter(stream, fieldnames=FIELDS)
                writer.writeheader()
                for rid, text in pairs:
                    writer.writerow(dict(zip(FIELDS, [rid, text, '1', '0', '1.0', '2026-01-01'])))
            with self.assertRaises(ValueError):
                self.store.ingest(path)
            self.assertEqual(self.store.status()['source_rows'], 1)
            self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM datasets').fetchone()[0], 1)
        self.assertEqual(self.store.db.execute('SELECT id FROM datasets').fetchone()[0], did)

    def test_configuration_and_verifier_role_invalidate_cache(self):
        did, cid = self.setup_jobs([('a', 'synthetic')])
        batch = self.store.claim(cid, 'one')
        call = self.store.dispatch(self.store.create_run(1), cid, 'one', batch, .1)
        self.store.finish(call, [prediction(batch[0])], cost=.01)
        self.assertEqual(self.store.prepare(did, dict(self.config)), cid)
        self.assertEqual(self.store.claim(cid, 'warm'), [])
        for delta in ({'prompt': 'v2'}, {'model': 'new-version'}, {'role': 'verify'}):
            new_cid = self.store.prepare(did, dict(self.config, **delta))
            self.assertNotEqual(new_cid, cid)
            self.assertEqual(len(self.store.claim(new_cid, 'new')), 1)

    def test_expired_undispatched_lease_recovers_but_in_flight_does_not(self):
        _, cid = self.setup_jobs([('a', 'one'), ('b', 'two')])
        with patch('review_pipeline.storage.time.time', return_value=100):
            batch = self.store.claim(cid, 'old', batch_size=1, lease_seconds=5)
        with patch('review_pipeline.storage.time.time', return_value=106):
            recovered = self.store.claim(cid, 'new', batch_size=1, lease_seconds=5)
            self.assertEqual(batch, recovered)
            call = self.store.dispatch(self.store.create_run(1), cid, 'new', recovered, .1)
        with patch('review_pipeline.storage.time.time', return_value=1000):
            other = self.store.claim(cid, 'another')
        self.assertEqual([r['review_id'] for r in other], ['b'])
        self.assertEqual(self.store.status(cid)['jobs']['in_flight'], 1)
        with self.assertRaises(ValueError):
            self.store.dispatch(self.store.create_run(1), cid, 'old', batch, .1)
        self.store.finish(call, [prediction(batch[0])], cost=.01)

    def test_reservation_cap_and_unknown_charge_block_further_dispatch(self):
        _, cid = self.setup_jobs([('a', 'one'), ('b', 'two')])
        a = self.store.claim(cid, 'a', batch_size=1)
        b = self.store.claim(cid, 'b', batch_size=1)
        run = self.store.create_run(.1)
        call = self.store.dispatch(run, cid, 'a', a, .08)
        with self.assertRaises(ValueError):
            self.store.dispatch(run, cid, 'b', b, .03)
        self.assertEqual(self.store.status(cid)['jobs'], {'claimed': 1, 'in_flight': 1})
        self.store.finish(call, error='timeout', cost=None, retry=True)
        ledger = self.store.status(cid)['runs'][0]
        self.assertAlmostEqual(ledger['reserved'], .08)
        self.assertEqual(ledger['spent'], 0)
        self.assertEqual(self.store.status(cid)['jobs']['uncertain'], 1)
        with self.assertRaises(ValueError):
            self.store.dispatch(run, cid, 'b', b, .03)
        with self.assertRaises(ValueError):
            self.store.finish(call, error='duplicate finish', cost=0)

    def test_malformed_prediction_rolls_back_all_outputs_and_accounting(self):
        _, cid = self.setup_jobs([('a', 'one'), ('b', 'two')])
        batch = self.store.claim(cid, 'worker')
        run = self.store.create_run(1)
        call = self.store.dispatch(run, cid, 'worker', batch, .1)
        mutations = [lambda rows: rows[1].update(review_id='foreign'),
                     lambda rows: rows[1].update(evidence_quote='invented'),
                     lambda rows: rows[1].update(severity=True),
                     lambda rows: rows[1].update(sentiment=float('nan'))]
        for mutate in mutations:
            rows = [prediction(r) for r in batch]
            mutate(rows)
            with self.assertRaises(ValueError):
                self.store.finish(call, rows, cost=.01)
            self.assertEqual(self.store.status(cid)['jobs'], {'in_flight': 2})
            ledger = self.store.status(cid)['runs'][0]
            self.assertEqual(ledger['spent'], 0)
            self.assertAlmostEqual(ledger['reserved'], .1)
        self.store.finish(call, [prediction(r) for r in batch], cost=.01)
        self.assertEqual(self.store.status(cid)['jobs'], {'completed': 2})
        self.assertAlmostEqual(self.store.status(cid)['runs'][0]['reserved'], 0)


    def test_dispatch_rejects_forged_source_expired_lease_and_duplicate_tasks(self):
        _, cid = self.setup_jobs([('a', 'original')])
        with patch('review_pipeline.storage.time.time', return_value=100):
            batch = self.store.claim(cid, 'worker', lease_seconds=5)
            run = self.store.create_run(1)
            for field, value in (('review_id', 'forged'), ('review_text', 'altered'), ('review_rating', '5')):
                forged = [dict(batch[0], **{field: value})]
                with self.assertRaisesRegex(ValueError, 'original review'):
                    self.store.dispatch(run, cid, 'worker', forged, .1)
            with self.assertRaisesRegex(ValueError, 'Duplicate tasks'):
                self.store.dispatch(run, cid, 'worker', batch + batch, .1)
        with patch('review_pipeline.storage.time.time', return_value=106):
            with self.assertRaisesRegex(ValueError, 'ownership'):
                self.store.dispatch(run, cid, 'worker', batch, .1)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0], 0)
        self.assertEqual(self.store.status(cid)['runs'][0]['reserved'], 0)
        self.assertEqual(self.store.db.execute('SELECT attempt_count FROM jobs').fetchone()[0], 0)

    def test_finish_lost_ownership_rolls_back_prior_batch_updates(self):
        _, cid = self.setup_jobs([('a', 'one'), ('b', 'two')])
        batch = self.store.claim(cid, 'worker')
        call = self.store.dispatch(self.store.create_run(1), cid, 'worker', batch, .1)
        self.store.db.execute("UPDATE jobs SET owner='unexpected' WHERE source_id='b'")
        for kwargs in ({'predictions': [prediction(r) for r in batch]}, {'error': 'failure', 'retry': True}):
            with self.assertRaisesRegex(ValueError, 'ownership'):
                self.store.finish(call, cost=.01, **kwargs)
            self.assertEqual(self.store.status(cid)['jobs'], {'in_flight': 2})
            self.assertEqual(self.store.db.execute('SELECT outcome FROM calls WHERE id=?', (call,)).fetchone()[0], 'in_flight')
            self.assertEqual(self.store.status(cid)['runs'][0]['spent'], 0)
            self.assertAlmostEqual(self.store.status(cid)['runs'][0]['reserved'], .1)

    def test_retry_count_is_bounded_and_failed_calls_retained(self):
        _, cid = self.setup_jobs([('a', 'one')])
        run = self.store.create_run(1)
        for attempt in range(2):
            batch = self.store.claim(cid, 'worker')
            self.assertEqual(len(batch), 1)
            call = self.store.dispatch(run, cid, 'worker', batch, .1)
            self.store.finish(call, error='known failure', retry=True, cost=.01)
            expected = 'pending' if attempt == 0 else 'quarantined'
            self.assertEqual(self.store.status(cid)['jobs'], {expected: 1})
        self.assertEqual(self.store.claim(cid, 'worker'), [])
        self.assertEqual(self.store.db.execute('SELECT attempt_count FROM jobs').fetchone()[0], 2)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM calls WHERE outcome='failed'").fetchone()[0], 2)
        self.assertAlmostEqual(self.store.status(cid)['runs'][0]['spent'], .02)

    def test_unsupported_entity_rejected_supported_casefold_term_accepted(self):
        _, cid = self.setup_jobs([('a', 'Spotify queue is frustrating')])
        batch = self.store.claim(cid, 'worker')
        call = self.store.dispatch(self.store.create_run(1), cid, 'worker', batch, .1)
        for entities in (['Apple'], ['']):
            row = dict(prediction(batch[0]), entities=entities)
            with self.assertRaisesRegex(ValueError, 'Unsupported entity'):
                self.store.finish(call, [row], cost=.01)
        self.store.finish(call, [dict(prediction(batch[0]), entities=['spotify'])], cost=.01)
        self.assertEqual(self.store.status(cid)['jobs'], {'completed': 1})

    def test_cross_dataset_export_refuses_missing_cache_original(self):
        _, cid = self.setup_jobs([('original', 'same exact text')])
        batch = self.store.claim(cid, 'worker')
        call = self.store.dispatch(self.store.create_run(1), cid, 'worker', batch, .1)
        self.store.finish(call, [prediction(batch[0])], cost=.01)
        subset, _ = self.ingest([('alias', 'same exact text')], name='subset.csv')
        self.assertEqual(self.store.prepare(subset, self.config), cid)
        with self.assertRaisesRegex(ValueError, 'Cache original is outside this dataset'):
            list(self.store.records(subset, cid))


if __name__ == '__main__':
    unittest.main()
