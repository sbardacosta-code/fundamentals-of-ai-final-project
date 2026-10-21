import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from review_pipeline.pilot_memo import MODEL, run_memo, load_api_key
from review_pipeline.storage import Store, FIELDS, atomic_json
from review_pipeline.ranking import rank


def setup_pilot(root, budget=.10):
    source = root / 'input.csv'
    with source.open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerow(dict(review_id='r-one', review_text='Music stopped.', review_rating='1',
                             review_likes='0', app_version='v', review_timestamp='2026-01-01'))
    store = Store(root / 'pilot.sqlite')
    dataset = store.ingest(source)['file_sha256']
    cid = store.prepare(dataset, {'role': 'enrich', 'model': 'synthetic-jev'})
    run_id = store.create_run(budget)
    jobs = store.claim(cid, 'test')
    call = store.dispatch(run_id, cid, 'test', jobs, .001)
    prediction = dict(review_id='r-one', topic='playback', intent='complaint', severity=4,
                      sentiment=-.8, entities=[], evidence_quote='Music stopped.', needs_review=False)
    store.finish(call, [prediction], usage={'input_tokens': 10, 'output_tokens': 0}, cost=.000001)
    (root / 'pilot_records.jsonl').write_text('\n'.join(json.dumps(row) for row in store.records(dataset, cid)))
    (root / 'membership.csv').write_text('issue_id,review_id\nplayback-failure,r-one\n')
    rank(root / 'pilot_records.jsonl', root / 'membership.csv', root / 'ranking.csv')
    atomic_json(root / 'manifest.json', {'run_id': run_id, 'identity': {'dataset_id': dataset, 'enrichment_config': cid},
                                       'budget_usd': budget, 'jev_stages_complete': True,
                                       'created_at': datetime.now(timezone.utc).isoformat()})
    for phase in ('cold', 'warm'):
        atomic_json(root / (phase + '.json'), {'jev_stages_complete': True, 'elapsed_seconds': .01,
                                             'stage_seconds': {'enrich': .005},
                                             'stages_completed': ['prepare', 'enrich', 'verify', 'group', 'rank']})
    store.close()


def good_client(pack, **kwargs):
    claim = pack['prompt_claims'][0]['claim_id']
    payload = {'recommendation': 'Investigate playback for {{' + claim + '}} complaint records.',
               'alternatives': ['Improve diagnostics first.'], 'limitations': ['Self-selected historical reviews limit inference.'],
               'claims': [claim], 'issue_ids': ['playback-failure'], 'review_ids': ['r-one']}
    return {'request_id': 'provider-test', 'model': MODEL, 'outcome': 'completed',
            'usage': {'input_tokens': 100, 'output_tokens': 20, 'input_tokens_details': {'cached_tokens': 50}},
            'text': json.dumps(payload), 'elapsed_seconds': .01, 'raw': {'synthetic': True}}


class PilotMemoTests(unittest.TestCase):
    def test_cold_shared_ledger_and_warm_zero_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            setup_pilot(root)
            cold = run_memo(root, execute_paid=True, client=good_client)
            self.assertTrue(cold['full_pilot_complete'])
            self.assertIsNone(cold['end_to_end_wall_seconds'])
            self.assertGreaterEqual(cold['active_execution_seconds'], .01)
            self.assertAlmostEqual(cold['ledger']['spent'], .000058)
            self.assertEqual(cold['ledger']['reserved'], 0)
            calls_before = (root / 'pilot_calls.jsonl').read_bytes()
            def forbidden(*args, **kwargs):
                self.fail('warm run made a provider call')
            warm = run_memo(root, phase='warm', client=forbidden)
            self.assertTrue(warm['memo']['cache_hit'])
            self.assertIsNone(warm['wall_elapsed_seconds'])
            self.assertIsNone(warm['pause_seconds'])
            again = run_memo(root, client=forbidden)
            self.assertAlmostEqual(again['elapsed_seconds'], .01 + again['memo_elapsed_seconds'])
            self.assertEqual(again['elapsed_seconds'], again['active_elapsed_seconds'])
            self.assertEqual(again['elapsed_seconds'], again['active_execution_seconds'])
            self.assertGreaterEqual(again['wall_elapsed_seconds'], 0)
            self.assertEqual((root / 'pilot_calls.jsonl').read_bytes(), calls_before)
            self.assertEqual(len(list((root / 'memo_raw').glob('*.json'))), 1)

    def test_cap_blocks_before_provider_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            setup_pilot(root, budget=.04)
            def forbidden(*args, **kwargs):
                self.fail('budget-exceeding call was dispatched')
            with self.assertRaisesRegex(ValueError, 'cap'):
                run_memo(root, execute_paid=True, client=forbidden)
            store = Store(root / 'pilot.sqlite')
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM calls WHERE json_extract(payload,'$.role')='memo'").fetchone()[0], 0)
            store.close()

    def test_unknown_usage_retains_reservation_and_blocks_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            setup_pilot(root)
            def unknown(pack, **kwargs):
                result = good_client(pack, **kwargs)
                result['usage'] = None
                return result
            with self.assertRaisesRegex(RuntimeError, 'Unknown usage'):
                run_memo(root, execute_paid=True, client=unknown)
            metadata = json.loads((root / 'cold.json').read_text())
            self.assertAlmostEqual(metadata['ledger']['reserved'], .06)
            with self.assertRaisesRegex(ValueError, 'Unresolved'):
                run_memo(root, execute_paid=True, retry_invalid=True, client=good_client)
            self.assertEqual(len(list((root / 'memo_raw').glob('*.json'))), 1)

    def test_validation_failure_is_billed_and_retry_is_explicit_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            setup_pilot(root, budget=.2)
            def invalid(pack, **kwargs):
                result = good_client(pack, **kwargs)
                result['text'] = '{}'
                return result
            with self.assertRaises(RuntimeError):
                run_memo(root, execute_paid=True, client=invalid)
            with self.assertRaisesRegex(ValueError, 'explicit retry'):
                run_memo(root, execute_paid=True, client=good_client)
            with self.assertRaises(RuntimeError):
                run_memo(root, execute_paid=True, retry_invalid=True, client=invalid)
            with self.assertRaisesRegex(ValueError, 'Global memo attempt cap'):
                run_memo(root, execute_paid=True, retry_invalid=True, client=good_client)
            calls = [json.loads(line) for line in (root / 'pilot_calls.jsonl').read_text().splitlines()]
            memos = [row for row in calls if row['role'] == 'memo']
            self.assertEqual(len(memos), 2)
            self.assertTrue(all(row['outcome'] == 'failed' and row['cost_usd'] > 0 for row in memos))
            self.assertEqual(len(list((root / 'memo_raw').glob('*.json'))), 2)

    def test_prompt_migration_cannot_reset_global_retry_allowance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            setup_pilot(root, budget=.2)
            def invalid(pack, **kwargs):
                result = good_client(pack, **kwargs)
                result['text'] = '{}'
                return result
            with self.assertRaises(RuntimeError):
                run_memo(root, execute_paid=True, client=invalid)
            with patch('review_pipeline.pilot_memo.file_sha', return_value='changed-config'):
                with self.assertRaisesRegex(ValueError, 'explicit retry'):
                    run_memo(root, execute_paid=True, client=good_client)
                result = run_memo(root, execute_paid=True, retry_invalid=True, client=good_client)
                self.assertTrue(result['memo_complete'])
            with patch('review_pipeline.pilot_memo.file_sha', return_value='changed-again'):
                with self.assertRaisesRegex(ValueError, 'Global memo attempt cap'):
                    run_memo(root, execute_paid=True, retry_invalid=True, client=good_client)
            calls = [json.loads(line) for line in (root / 'pilot_calls.jsonl').read_text().splitlines()]
            self.assertEqual(sum(row['role'] == 'memo' for row in calls), 2)

    def test_env_loader_is_literal_whitelisted_and_preserves_existing_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text('UNRELATED_SECRET=ignored\nexport OPENAI_API_KEY="literal-$(not-executed)"\n')
            with patch.dict(os.environ, {}, clear=True):
                load_api_key(path)
                self.assertEqual(dict(os.environ), {'OPENAI_API_KEY': 'literal-$(not-executed)'})
                os.environ['OPENAI_API_KEY'] = 'existing'
                load_api_key(path)
                self.assertEqual(os.environ['OPENAI_API_KEY'], 'existing')

    def test_interrupted_call_is_not_automatically_repeated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            setup_pilot(root)
            def interrupt(*args, **kwargs):
                raise KeyboardInterrupt()
            with self.assertRaises(KeyboardInterrupt):
                run_memo(root, execute_paid=True, client=interrupt)
            with self.assertRaisesRegex(ValueError, 'Unresolved'):
                run_memo(root, execute_paid=True, retry_invalid=True, client=good_client)


if __name__ == '__main__':
    unittest.main()
