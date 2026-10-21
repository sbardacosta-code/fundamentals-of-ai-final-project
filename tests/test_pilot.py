import csv
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from review_pipeline.pilot import run_pilot, main, load_api_key
from review_pipeline.storage import FIELDS
from review_pipeline.jev import JevTransportError


class FakeJev:
    def __init__(self, missing_usage=False, fail=False):
        self.calls = []
        self.missing_usage = missing_usage
        self.fail = fail

    def evaluate(self, records, role='enrich'):
        self.calls.append((role, [r['review_id'] for r in records]))
        if self.fail:
            raise JevTransportError('synthetic timeout', outcome_unknown=True)
        results = [dict(review_id=r['review_id'], topic='other', intent='complaint', severity=2,
                        sentiment=-.5, entities=[], evidence_quote=r['review_text'], needs_review=False) for r in records]
        raw = {'model': 'jev-1.13.0', 'usage': {'input_tokens': 100, 'output_tokens': 5}}
        if self.missing_usage:
            del raw['usage']
        return results, raw


class FakeGroup:
    def __init__(self):
        self.calls = []

    def evaluate_group(self, records, catalog):
        self.calls.append([r['review_id'] for r in records])
        return ([dict(review_id=r['review_id'], issue_id='residual-other') for r in records],
                {'model': 'jev-1.13.0', 'usage': {'input_tokens': 50, 'output_tokens': 2}})


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.input = self.root / 'cost_100.csv'
        self.output = self.root / 'pilot'
        with self.input.open('w', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            for i in range(100):
                writer.writerow(dict(zip(FIELDS, [str(i), 'synthetic text ' + str(i % 20), '1', '0', '1', '2026-01-01'])))

    def tearDown(self):
        self.tmp.cleanup()

    def run_it(self, client, group, **kwargs):
        return run_pilot(self.input, self.output, enrich_client=client, group_client=group, **kwargs)

    def test_complete_cold_and_zero_call_warm_with_deduplication(self):
        client, group = FakeJev(), FakeGroup()
        cold = self.run_it(client, group)
        self.assertEqual(cold['status_counts'], {'completed': 100})
        self.assertEqual(sum(len(ids) for role, ids in client.calls if role == 'enrich'), 20)
        self.assertEqual(sum(map(len, group.calls)), 20)
        self.assertIn('rank', cold['stages_completed'])
        self.assertNotIn('memo', cold['stages_completed'])
        before = (len(client.calls), len(group.calls))
        warm = self.run_it(client, group, phase='warm')
        self.assertEqual(before, (len(client.calls), len(group.calls)))
        self.assertEqual(cold['run_id'], warm['run_id'])
        self.assertEqual(warm['ledger']['reserved'], 0)
        rows = [json.loads(line) for line in (self.output / 'pilot_records.jsonl').read_text().splitlines()]
        self.assertEqual(len(rows), 100)
        self.assertEqual(sum('cache_source_id' in r for r in rows), 80)
        with (self.output / 'membership.csv').open() as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 100)

    def test_interruption_resumes_same_budget_without_recalling_completed(self):
        client, group = FakeJev(), FakeGroup()
        with self.assertRaises(InterruptedError):
            self.run_it(client, group, stop_after_batches=1)
        manifest = json.loads((self.output / 'manifest.json').read_text())
        first_ids = set(client.calls[0][1])
        result = self.run_it(client, group)
        self.assertEqual(result['run_id'], manifest['run_id'])
        later_enrich = {rid for role, ids in client.calls[1:] if role == 'enrich' for rid in ids}
        self.assertTrue(first_ids.isdisjoint(later_enrich))
        self.assertEqual(result['ledger']['budget'], 5)
        before = json.loads((self.output / 'checkpoint_before.json').read_text())
        after = json.loads((self.output / 'checkpoint_after.json').read_text())
        self.assertTrue(set(before['completed_ids']) < set(after['completed_ids']))
        self.assertGreater(result['stage_times_seconds']['enrich'], 0)
        calls = [json.loads(line) for line in (self.output / 'pilot_calls.jsonl').read_text().splitlines()]
        self.assertTrue(all(call['run_phase'] == 'cold' for call in calls))
        self.assertEqual(calls[0]['phase'], 'initial')
        self.assertTrue(any(call['phase'] == 'resume' for call in calls[1:]))

    def test_unknown_usage_retains_reservation_and_blocks_restart(self):
        client, group = FakeJev(missing_usage=True), FakeGroup()
        with self.assertRaisesRegex(RuntimeError, 'Unknown Jev usage'):
            self.run_it(client, group)
        cold = json.loads((self.output / 'cold.json').read_text())
        self.assertAlmostEqual(cold['ledger']['reserved'], .01)
        with self.assertRaisesRegex(ValueError, 'Unresolved'):
            self.run_it(client, group)
        self.assertEqual(len(client.calls), 1)
        calls = [json.loads(line) for line in (self.output / 'pilot_calls.jsonl').read_text().splitlines()]
        self.assertEqual(calls[0]['raw_response'], {'model': 'jev-1.13.0'})

    def test_transport_timeout_saved_and_never_automatically_rebilled(self):
        client, group = FakeJev(fail=True), FakeGroup()
        with self.assertRaises(JevTransportError):
            self.run_it(client, group)
        with self.assertRaisesRegex(ValueError, 'Unresolved'):
            self.run_it(client, group)
        self.assertEqual(len(client.calls), 1)

    def test_warm_without_cold_and_changed_input_refuse_calls(self):
        client, group = FakeJev(), FakeGroup()
        with self.assertRaises(ValueError):
            self.run_it(client, group, phase='warm')
        self.assertEqual(client.calls, [])
        self.run_it(client, group)
        before = len(client.calls)
        self.input.write_text(self.input.read_text().replace('synthetic text 0', 'changed text 0'))
        with self.assertRaises(ValueError):
            self.run_it(client, group)
        self.assertEqual(len(client.calls), before)


    def test_cli_requires_explicit_paid_flag_and_env_loader_does_not_execute(self):
        with patch('sys.argv', ['pilot']), patch('sys.stderr'), patch('review_pipeline.pilot.run_pilot') as mocked:
            with self.assertRaises(SystemExit):
                main()
            mocked.assert_not_called()
        path = self.root / '.env'
        path.write_text('UNRELATED=do-not-load\nTYPESAFE_API_KEY="literal-$(do-not-execute)"\n')
        with patch.dict(os.environ, {}, clear=True):
            load_api_key(path)
            self.assertEqual(os.environ['TYPESAFE_API_KEY'], 'literal-$(do-not-execute)')
            self.assertNotIn('UNRELATED', os.environ)

    def test_memo_metadata_survives_later_cached_cold_invocation(self):
        client, group = FakeJev(), FakeGroup()
        self.run_it(client, group)
        path = self.output / 'cold.json'
        meta = json.loads(path.read_text())
        meta['stages_completed'].append('memo')
        meta['stage_seconds']['memo'] = .3
        meta.update(full_pilot_complete=True, remaining=[], memo_attempts=[{'status': 'saved'}])
        path.write_text(json.dumps(meta))
        result = self.run_it(client, group)
        self.assertIn('memo', result['stages_completed'])
        self.assertTrue(result['full_pilot_complete'])
        self.assertEqual(result['stage_times_seconds']['memo'], .3)
        self.assertEqual(result['memo_attempts'], [{'status': 'saved'}])


if __name__ == '__main__':
    unittest.main()
