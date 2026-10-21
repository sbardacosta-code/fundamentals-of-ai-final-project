import csv
import json
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path

from review_pipeline.jev import JevHTTPError, JevTransportError, JevValidationError
from review_pipeline.scale import RateLimiter, backoff_seconds, run_checkpoint
from review_pipeline.scale_plan import (duplicate_profile, project, ranked_rows,
                                        verify_selected, write_sample)
from review_pipeline.storage import FIELDS, Store


class FakeJev:
    """Synthetic provider; thread-safe and never networked."""

    def __init__(self, fail_first=None):
        self.lock = threading.Lock()
        self.calls = []
        self.fail_first = list(fail_first or [])

    def evaluate(self, records, role='enrich'):
        with self.lock:
            self.calls.append((role, [r['review_id'] for r in records]))
            failure = self.fail_first.pop(0) if self.fail_first else None
        if failure:
            raise failure
        intent = lambda r: 'praise' if 'good' in r['review_text'] else 'complaint'
        results = [dict(review_id=r['review_id'], topic='other', intent=intent(r), severity=2,
                        sentiment=-.5, entities=[], evidence_quote=r['review_text'], needs_review=False)
                   for r in records]
        return results, {'model': 'jev-1.13.0', 'usage': {'input_tokens': 1000, 'output_tokens': 5}}


class FakeGroup:
    def __init__(self):
        self.lock = threading.Lock()
        self.calls = []

    def evaluate_group(self, records, catalog):
        with self.lock:
            self.calls.append([r['review_id'] for r in records])
        return ([dict(review_id=r['review_id'], issue_id='residual-other') for r in records],
                {'model': 'jev-1.13.0', 'usage': {'input_tokens': 100, 'output_tokens': 2}})


def write_rows(path, count, unique=None):
    with Path(path).open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for i in range(count):
            text = ('good app ' if i % 3 == 0 else 'it crashes ') + str(i % (unique or count))
            writer.writerow(dict(zip(FIELDS, [f'id{i:05d}', text, '1', '0', '', '2026-01-01'])))


class ScaleRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.small = self.root / 'small.csv'
        self.large = self.root / 'large.csv'
        write_rows(self.small, 40, unique=30)
        write_rows(self.large, 120, unique=30)  # Superset: first 40 rows identical.

    def tearDown(self):
        self.tmp.cleanup()

    def run_it(self, path, name, client, group, **kwargs):
        options = dict(root=self.root / 'scale', budget_usd=1.0, workers=3, execute_paid=True,
                       verify_rate='0.3', requests_per_second=1000, enrich_client=client, group_client=group)
        options.update(kwargs)
        return run_checkpoint(path, name, **options)

    def test_requires_explicit_paid_flag_and_bounded_settings(self):
        with self.assertRaises(ValueError):
            run_checkpoint(self.small, 'x', root=self.root / 's', budget_usd=1.0)
        with self.assertRaises(ValueError):
            run_checkpoint(self.small, 'x', root=self.root / 's', budget_usd=11, execute_paid=True)
        with self.assertRaises(ValueError):
            run_checkpoint(self.small, 'x', root=self.root / 's', budget_usd=1, workers=9, execute_paid=True)

    def test_parallel_workers_complete_every_stage_once_per_unique_text(self):
        client, group = FakeJev(), FakeGroup()
        result = self.run_it(self.small, 'cp', client, group)
        self.assertTrue(result['stages_complete'], result['error'])
        self.assertEqual(result['status_counts'], {'completed': 40})
        sent = [rid for role, ids in client.calls if role == 'enrich' for rid in ids]
        self.assertEqual(len(sent), 30)  # Exact duplicates are cached, not resent.
        self.assertEqual(result['cache_reused_rows'], 10)
        workers = {json.loads(l)['worker'].split('-')[1] for l in (self.root / 'scale/cp/calls.jsonl').read_text().splitlines()}
        self.assertGreater(len(workers), 1)
        manifest = json.loads((self.root / 'scale/cp/manifest.json').read_text())
        expected = [f'id{i:05d}' for i in range(40) if verify_selected(f'id{i:05d}', '0.3')]
        self.assertEqual(manifest['verification_ids'], expected)
        for name in ('records.jsonl', 'membership.csv', 'ranking.csv', 'verification_comparison.json', 'checkpoint_after.json'):
            self.assertTrue((self.root / 'scale/cp' / name).exists(), name)
        ledger = result['ledger']
        self.assertAlmostEqual(ledger['reserved'], 0)
        self.assertLessEqual(ledger['spent'], 1.0)

    def test_nested_checkpoint_pays_only_for_new_texts(self):
        client, group = FakeJev(), FakeGroup()
        self.run_it(self.small, 'cp500', client, group)
        before = len(client.calls)
        result = self.run_it(self.large, 'cp10k', client, group)
        self.assertTrue(result['stages_complete'], result['error'])
        new_enrich = [rid for role, ids in client.calls[before:] if role == 'enrich' for rid in ids]
        self.assertEqual(new_enrich, [])  # Same 30 texts: fully cached.
        self.assertEqual(result['status_counts'], {'completed': 120})

    def test_interrupt_and_resume_without_repeating_completed_batches(self):
        client, group = FakeJev(), FakeGroup()
        first = self.run_it(self.small, 'cp', client, group, workers=1, stop_after_batches=2)
        self.assertIn('Requested checkpoint', first['error'])
        before = json.loads((self.root / 'scale/cp/checkpoint_before.json').read_text())['completed_ids']
        self.assertTrue(before)
        sent_before = {rid for role, ids in client.calls for rid in ids if role == 'enrich'}
        second = self.run_it(self.small, 'cp', client, group, workers=2)
        self.assertTrue(second['stages_complete'], second['error'])
        sent_after = [rid for role, ids in client.calls[2:] for rid in ids if role == 'enrich']
        self.assertFalse(sent_before & set(sent_after))
        calls = [json.loads(l) for l in (self.root / 'scale/cp/calls.jsonl').read_text().splitlines()]
        self.assertIn('resume', {c['phase'] for c in calls if c['role'] == 'enrich'})
        after = json.loads((self.root / 'scale/cp/checkpoint_after.json').read_text())['completed_ids']
        self.assertGreater(len(after), len(before))

    def test_cap_cannot_change_on_resume_and_stops_dispatch(self):
        client, group = FakeJev(), FakeGroup()
        result = self.run_it(self.small, 'cp', client, group, budget_usd=0.009, workers=2)
        self.assertFalse(result['stages_complete'])
        self.assertIn('Spending cap', result['error'])
        self.assertLessEqual(result['ledger']['spent'] + result['ledger']['reserved'], 0.009)
        with self.assertRaisesRegex(ValueError, 'fixed at creation'):
            self.run_it(self.small, 'cp', client, group, budget_usd=0.5)

    def test_unknown_outcome_holds_reservation_and_blocks_resume(self):
        client = FakeJev(fail_first=[JevTransportError('timeout', retryable=True, outcome_unknown=True)])
        result = self.run_it(self.small, 'cp', client, FakeGroup(), workers=1)
        self.assertIn('Unknown provider outcome', result['error'])
        self.assertGreater(result['ledger']['reserved'], 0)
        with self.assertRaisesRegex(ValueError, 'reconciliation'):
            self.run_it(self.small, 'cp', FakeJev(), FakeGroup())

    def test_rate_limit_is_unbilled_retried_and_not_counted_as_attempt(self):
        client = FakeJev(fail_first=[JevHTTPError('HTTP 429', status=429, retryable=True, retry_after='0')])
        result = self.run_it(self.small, 'cp', client, FakeGroup(), workers=1)
        self.assertTrue(result['stages_complete'], result['error'])
        self.assertEqual(result['status_counts'], {'completed': 40})
        failed = [json.loads(l) for l in (self.root / 'scale/cp/calls.jsonl').read_text().splitlines()
                  if json.loads(l)['outcome'] == 'failed']
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]['cost_usd'], 0.0)

    def test_invalid_output_is_billed_and_retried_once(self):
        bad = JevValidationError('bad', raw_response={'model': 'jev-1.13.0', 'usage': {'input_tokens': 1000, 'output_tokens': 0}})
        result = self.run_it(self.small, 'cp', FakeJev(fail_first=[bad]), FakeGroup(), workers=1)
        self.assertTrue(result['stages_complete'], result['error'])
        self.assertIn('enrich:failed', result['calls_by_role_outcome'])
        self.assertGreater(Decimal(result['cost_by_role_usd']['enrich']), Decimal('0.000042') * 6)


class RateLimiterTests(unittest.TestCase):
    def test_spacing_and_cooldown(self):
        now = [0.0]
        slept = []
        limiter = RateLimiter(2, sleep=lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)), clock=lambda: now[0])
        limiter.wait(); limiter.wait(); limiter.cooldown(5); limiter.wait()
        self.assertEqual(slept[0], 0.5)
        self.assertGreaterEqual(sum(slept), 5)
        self.assertEqual(backoff_seconds(3, retry_after='2'), 2.0)
        self.assertLessEqual(backoff_seconds(10), 30)


class ScalePlanTests(unittest.TestCase):
    def test_duplicates_counted_on_exact_unmodified_text(self):
        rows = [dict(review_id=str(i), review_text=t) for i, t in enumerate(['a', 'a', 'a ', 'b'])]
        profile = duplicate_profile(rows)
        self.assertEqual((profile['unique_texts'], profile['duplicate_rows']), (3, 1))

    def test_verification_selection_is_nested_and_deterministic(self):
        ids = [str(i) for i in range(5000)]
        small = {i for i in ids if verify_selected(i, '0.05')}
        large = {i for i in ids if verify_selected(i, '0.10')}
        self.assertTrue(small <= large)
        self.assertTrue(150 < len(small) < 350)

    def test_projection_arithmetic_and_fixed_memo(self):
        units = dict(enrich_per_unique_text=Decimal('0.00005'), verify_per_verified_text=Decimal('0.00005'),
                     group_per_unique_complaint_text=Decimal('0.00001'), memo_per_run=Decimal('0.005'),
                     complaint_share_of_unique=Decimal('0.5'))
        result = project(dict(unique_texts=1000, verified_unique_texts=100), units)
        self.assertEqual(result['total_usd'], '0.065000')
        doubled = project(dict(unique_texts=2000, verified_unique_texts=200), units)
        self.assertEqual(doubled['stage_usd']['memo'], '0.005000')
        conservative = project(dict(unique_texts=1000, verified_unique_texts=100), units, multiplier='1.25')
        self.assertEqual(conservative['stage_usd']['enrich'], '0.062500')

    def test_ranked_rows_excludes_empty_invalid_and_duplicate_ids(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'full.csv'
            with path.open('w', newline='', encoding='utf-8') as handle:
                writer = csv.DictWriter(handle, fieldnames=FIELDS)
                writer.writeheader()
                for rid, text, rating in (('a', 'x', '5'), ('b', ' ', '5'), ('c', 'y', '0'), ('a', 'z', '5'), ('d', 'w', '1')):
                    writer.writerow(dict(zip(FIELDS, [rid, text, rating, '0', '', '2026-01-01'])))
            rows, excluded = ranked_rows(path)
            self.assertEqual((sorted(r['review_id'] for r in rows), excluded), (['a', 'd'], 3))
            sha = write_sample(rows, Path(temp) / 'out' / 'sample.csv')
            self.assertEqual(len(sha), 64)


if __name__ == '__main__':
    unittest.main()
