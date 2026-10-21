import copy
import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from review_pipeline.jev import (MODEL, JevClient, JevError, JevHTTPError,
    JevTransportError, JevValidationError, build_request, parse_response)


def response_for(records):
    request = build_request(records)
    answers = {}
    for key, question in request['questions'].items():
        if question['type'] == 'choice':
            options = question['criteria']
            selected = next(iter(options))
            answers[key] = {'type': 'choice', 'choice': selected,
                            'probabilities': {k: float(k == selected) for k in options}, 'confidence': 1.0}
        elif question['type'] == 'score':
            answers[key] = {'type': 'score', 'score': 2.5, 'confidence': 0.6,
                            'probabilities': {'0': 0, '1': 0, '2': .5, '3': .5, '4': 0},
                            'legend': {str(i): v for i, v in enumerate(question['criteria'])}}
        else:
            answers[key] = {'type': 'noul', 'noul': 0.2}
    return {'model': MODEL, 'answers': answers, 'usage': {'input_tokens': 123, 'new_vendor_metric': 9}}


class JevTests(unittest.TestCase):
    def setUp(self):
        self.records = [{'review_id': 'id/1', 'review_text': '  cannot log in 😢  '},
                        {'review_id': 'r0_topic', 'review_text': 'Great sound!'}]

    def test_blind_verifier_input_and_explicit_row_targeting(self):
        records = [dict(self.records[0], topic='SECRET_PRIOR', human_label='SECRET_GOLD', review_rating='1')]
        request = build_request(records, role='verify')
        self.assertEqual(request['state']['records'], [self.records[0]])
        for question in request['questions'].values():
            self.assertIn('state.records[0].review_text', question['instructions'])
            self.assertIn('Independently verify', question['instructions'])
        self.assertNotIn('SECRET', json.dumps(request))

    def test_batch_limits_and_ids(self):
        for records in ([], self.records * 2, [{'review_id': 'a', 'review_text': ' '}],
                        [{'review_id': str(i), 'review_text': 'x'} for i in range(51)]):
            with self.assertRaises(ValueError):
                build_request(records)
        self.assertEqual(len(build_request([{'review_id': str(i), 'review_text': 'x'} for i in range(50)])['questions']), 250)

    def test_round_trip_maps_ids_and_preserves_quote(self):
        raw = response_for(self.records)
        results = parse_response(raw, self.records)
        self.assertEqual([x['review_id'] for x in results], ['id/1', 'r0_topic'])
        self.assertEqual(results[0]['evidence_quote'], self.records[0]['review_text'])
        self.assertEqual(results[0]['sentiment'], .25)
        self.assertEqual(results[0]['severity'], 1)
        self.assertFalse(results[0]['needs_review'])
        self.assertEqual(results[0]['entities'], [])

    def test_rejects_misalignment_and_malformed_values_atomically(self):
        raw = response_for(self.records)
        mutations = [
            lambda r: r['answers'].pop('r1_topic'),
            lambda r: r['answers'].update({'foreign_id': {}}),
            lambda r: r['answers']['r1_topic'].update(choice='invalid'),
            lambda r: r['answers']['r1_topic'].update(choice=[]),
            lambda r: r['answers']['r1_topic'].update(confidence=float('nan')),
            lambda r: r['answers']['r1_sentiment'].update(score=True),
            lambda r: r['answers']['r1_sentiment'].update(score=3),
            lambda r: r['answers']['r1_ambiguity'].update(noul=1.1),
            lambda r: r['answers']['r1_topic']['probabilities'].update(access=.2),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                bad = copy.deepcopy(raw)
                mutate(bad)
                with self.assertRaises(JevValidationError) as caught:
                    parse_response(bad, self.records)
                self.assertIs(caught.exception.raw_response, bad)

    def test_usage_preserved_without_guessing(self):
        raw = response_for(self.records)
        with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(raw).encode())) as mock:
            parsed, actual = JevClient('test-only-key').evaluate(self.records)
        self.assertEqual(actual['usage'], raw['usage'])
        self.assertEqual(mock.call_count, 1)
        del raw['usage']
        with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(raw).encode())):
            _, actual = JevClient('test-only-key').evaluate(self.records)
        self.assertNotIn('usage', actual)

    def test_error_taxonomy_and_no_hidden_retries(self):
        for status, retryable in ((401, False), (422, False), (429, True), (529, True)):
            error = urllib.error.HTTPError('https://api.typesafe.ai', status, 'error', {'Retry-After': '2'}, None)
            with patch('urllib.request.urlopen', side_effect=error) as mock:
                with self.assertRaises(JevHTTPError) as caught:
                    JevClient('test-only-key').evaluate(self.records)
            self.assertEqual(caught.exception.retryable, retryable)
            self.assertEqual(caught.exception.retry_after, '2')
            self.assertEqual(mock.call_count, 1)
        with patch('urllib.request.urlopen', side_effect=TimeoutError):
            with self.assertRaises(JevTransportError) as caught:
                JevClient('test-only-key').evaluate(self.records)
        self.assertTrue(caught.exception.outcome_unknown)

    def test_no_key_no_network_and_model_mismatch(self):
        with patch('urllib.request.urlopen') as mock:
            with self.assertRaises(JevError):
                JevClient('').evaluate(self.records)
            mock.assert_not_called()
        raw = response_for(self.records)
        raw['model'] = 'different-model'
        with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(raw).encode())):
            with self.assertRaises(JevValidationError) as caught:
                JevClient('test-only-key').evaluate(self.records)
        self.assertEqual(caught.exception.raw_response, raw)


    def test_independently_rounded_provider_scores_and_probabilities(self):
        raw = response_for(self.records)
        raw['answers']['r0_sentiment'].update(
            score=.39, probabilities={'0': .62, '1': .38, '2': 0, '3': 0, '4': 0})
        raw['answers']['r1_sentiment'].update(
            score=2.27, probabilities={'0': .08, '1': .17, '2': .33, '3': .26, '4': .16})
        results = parse_response(raw, self.records)
        self.assertAlmostEqual(results[0]['sentiment'], -.805)
        self.assertAlmostEqual(results[1]['sentiment'], .135)
        for middle in (.32, .34):  # rounded distributions sum to .99 and 1.01
            raw['answers']['r1_sentiment']['probabilities']['2'] = middle
            parse_response(raw, self.records)
        bad = copy.deepcopy(raw)
        bad['answers']['r1_sentiment']['score'] = 2.4
        with self.assertRaises(JevValidationError):
            parse_response(bad, self.records)
        bad = copy.deepcopy(raw)
        bad['answers']['r1_sentiment']['probabilities']['2'] = .4
        with self.assertRaises(JevValidationError):
            parse_response(bad, self.records)

    def test_legend_cannot_relabel_the_sentiment_scale(self):
        raw = response_for(self.records)
        raw['answers']['r0_sentiment']['legend']['0'] = 'Strongly positive'
        with self.assertRaisesRegex(JevValidationError, 'legend'):
            parse_response(raw, self.records)


if __name__ == '__main__':
    unittest.main()
