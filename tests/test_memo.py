import csv
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from review_pipeline.memo import build_evidence, save_claims, make_prompt, validate_and_render, call_openai


def evidence(count=1):
    records, membership, ranking = [], [], []
    for issue in range(count):
        iid = f'issue-{issue}'
        for review in range(4):
            rid = f'r-{issue}-{review}'
            records.append({'review_id': rid, 'status': 'completed', 'intent': 'complaint',
                            'topic': 'playback', 'severity': 4, 'review_text': 'Music stopped.'})
            membership.append({'issue_id': iid, 'review_id': rid})
        ranking.append({'rank': issue + 1, 'issue_id': iid, 'complaint_count': 4,
                        'severity_sum': 16, 'mean_severity': '4.000000', 'priority_score': 16})
    return build_evidence(records, membership, ranking)


def memo(pack):
    claim = pack['prompt_claims'][0]['claim_id']
    return {'recommendation': 'Investigate playback failures affecting {{' + claim + '}} complaint records.',
            'alternatives': ['Start with diagnostics before changing playback behavior.'],
            'limitations': ['Historical self-selected reviews cannot establish population prevalence.'],
            'claims': [claim], 'issue_ids': ['issue-0'], 'review_ids': ['r-0-0']}


class MemoTests(unittest.TestCase):
    def test_bounded_evidence_full_ledger_and_csv(self):
        pack = evidence(6)
        self.assertEqual(len(pack['issues']), 5)
        self.assertTrue(all(len(issue['reviews']) == 3 for issue in pack['issues']))
        self.assertEqual(len(pack['claims']), 24)
        self.assertEqual(len(pack['prompt_claims']), 20)
        _, input_text = make_prompt(pack)
        self.assertNotIn('issue-5', input_text)
        self.assertEqual(pack['evidence_sha256'], evidence(6)['evidence_sha256'])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'claims.csv'
            save_claims(pack, path)
            with path.open() as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 24)

    def test_prompt_contains_a_valid_concrete_placeholder_example(self):
        pack = evidence()
        instructions, _ = make_prompt(pack)
        example = json.loads(instructions.split('claims.\n', 1)[1])
        self.assertIn('at least one quantitative sentence', instructions)
        self.assertIn('hypothesis to test', instructions)
        self.assertEqual(len(example['claims']), 1)
        self.assertIn('{{' + example['claims'][0] + '}}', example['recommendation'])
        self.assertEqual(pack['version'], 'memo-claims-v2')
        validate_and_render(example, pack)

    def test_claim_values_are_inserted_by_code(self):
        pack = evidence()
        result = validate_and_render(json.dumps(memo(pack)), pack)
        self.assertIn('affecting 4 [claim_', result['recommendation'])
        self.assertNotIn('{{', result['recommendation'])

    def test_unknown_ids_raw_numbers_and_untracked_claims_rejected(self):
        pack = evidence()
        for field, value in [('claims', ['fake']), ('issue_ids', ['unknown']), ('review_ids', ['unseen']),
                             ('recommendation', 'Fix 4 records.'), ('recommendation', 'Fix four {{unknown}} records.'),
                             ('claims', []), ('review_ids', [])]:
            payload = memo(pack)
            payload[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_and_render(payload, pack)

    def test_ranking_tampering_rejected(self):
        records = [{'review_id': 'r', 'status': 'completed', 'intent': 'complaint', 'severity': 3,
                    'evidence_quote': 'slow'}]
        membership = [{'review_id': 'r', 'issue_id': 'slow'}]
        ranking = [{'rank': 1, 'issue_id': 'slow', 'complaint_count': 1, 'severity_sum': 9,
                    'mean_severity': '3.000000', 'priority_score': 3}]
        with self.assertRaises(ValueError):
            build_evidence(records, membership, ranking)
        ranking[0]['severity_sum'] = 3
        pack = build_evidence(records, membership, ranking)
        self.assertTrue(pack['issues'][0]['reviews'][0]['excerpt_only'])

    def test_no_api_calls_without_explicit_gate(self):
        with patch('review_pipeline.memo.urlopen') as transport:
            with self.assertRaises(ValueError):
                call_openai(evidence(), model='synthetic-model')
            transport.assert_not_called()

    def test_mock_transport_preserves_usage_and_model(self):
        response = {'id': 'resp-test', 'status': 'completed', 'model': 'synthetic-exact',
                    'usage': {'input_tokens': 101, 'output_tokens': 10},
                    'output': [{'type': 'reasoning'}, {'type': 'message', 'content': [
                        {'type': 'output_text', 'text': '{"not":"yet validated"}'}]}]}
        class Response(io.BytesIO):
            headers = {'x-request-id': 'req-test'}
        def fake_transport(request, timeout):
            body = json.loads(request.data)
            self.assertEqual(body['model'], 'synthetic-model')
            self.assertFalse(body['store'])
            fmt = body['text']['format']
            self.assertEqual(fmt['type'], 'json_schema')
            self.assertTrue(fmt['strict'])
            self.assertFalse(fmt['schema']['additionalProperties'])
            self.assertEqual(set(fmt['schema']['required']), {'recommendation', 'alternatives', 'limitations', 'claims', 'issue_ids', 'review_ids'})
            return Response(json.dumps(response).encode())
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'synthetic-key'}), patch('review_pipeline.memo.urlopen', fake_transport):
            result = call_openai(evidence(), model='synthetic-model', allow_paid=True)
        self.assertEqual(result['raw'], response)
        self.assertEqual(result['model'], 'synthetic-exact')
        self.assertEqual(result['usage'], response['usage'])
        self.assertEqual(result['request_id'], 'req-test')
        self.assertEqual(result['text'], '{"not":"yet validated"}')


if __name__ == '__main__':
    unittest.main()
