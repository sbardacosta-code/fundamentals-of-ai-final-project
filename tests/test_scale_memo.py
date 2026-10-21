import json
import tempfile
import unittest
from pathlib import Path

from review_pipeline.scale import run_checkpoint
from review_pipeline.scale_memo import build_request, price, run_memo
from tests.test_scale import FakeGroup, FakeJev, write_rows


def good_client(request):
    evidence = json.loads(request['messages'][0]['content'])
    claim = evidence['claims'][0]
    issue = next(i for i in evidence['issues'] if i['issue_id'] == claim['issue_id'])
    memo = {'recommendation': 'Investigate first. Count {{' + claim['claim_id'] + '}}.',
            'alternatives': ['Run a focused user study.'], 'limitations': ['Self-selected reviews.'],
            'claims': [claim['claim_id']], 'issue_ids': [claim['issue_id']],
            'review_ids': [issue['reviews'][0]['review_id']]}
    return dict(request_id='req_1', model='claude-haiku-5-5', stop_reason='end_turn', text=json.dumps(memo),
                usage=dict(input_tokens=3000, output_tokens=800, cache_read_input_tokens=0, cache_creation_input_tokens=0))


class ScaleMemoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        write_rows(self.root / 'in.csv', 40, unique=30)
        run_checkpoint(self.root / 'in.csv', 'cp', root=self.root / 'scale', budget_usd=1.0, workers=2,
                       execute_paid=True, requests_per_second=1000, enrich_client=FakeJev(), group_client=FakeGroup())

    def tearDown(self):
        self.tmp.cleanup()

    def test_refuses_without_paid_flag_then_caches_success(self):
        with self.assertRaisesRegex(ValueError, 'execute-paid'):
            run_memo('cp', root=self.root / 'scale', client=good_client)
        result = run_memo('cp', root=self.root / 'scale', execute_paid=True, client=good_client)
        self.assertEqual(result['state'], 'succeeded')
        self.assertAlmostEqual(result['cost_usd'], 0.0007)
        self.assertAlmostEqual(result['ledger']['reserved'], 0)
        warm = run_memo('cp', root=self.root / 'scale', client=lambda r: self.fail('no call on warm'))
        self.assertTrue(warm['cached'])

    def test_invalid_output_is_billed_and_retry_is_bounded(self):
        bad = lambda r: dict(good_client(r), text='{"recommendation": "x"}')
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                run_memo('cp', root=self.root / 'scale', execute_paid=True, client=bad)
        with self.assertRaisesRegex(ValueError, 'reconcile'):
            run_memo('cp', root=self.root / 'scale', execute_paid=True, client=bad)

    def test_transport_failure_holds_reservation(self):
        def boom(request):
            raise OSError('timeout')
        with self.assertRaises(RuntimeError):
            run_memo('cp', root=self.root / 'scale', execute_paid=True, client=boom)
        with self.assertRaises(ValueError):
            run_memo('cp', root=self.root / 'scale', execute_paid=True, client=good_client)

    def test_request_shape_and_pricing(self):
        request = build_request({'version': 'v', 'issues': [], 'selection': {}, 'overlapping_membership': False,
                                 'evidence_sha256': 'x', 'prompt_claims': []})
        self.assertEqual(request['model'], 'claude-haiku-5-5')
        self.assertEqual(request['output_config']['format']['type'], 'json_schema')
        self.assertIsNone(price({'input_tokens': 1, 'output_tokens': 1, 'cache_creation_input_tokens': 5}))


if __name__ == '__main__':
    unittest.main()
