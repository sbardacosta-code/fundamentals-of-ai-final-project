import copy
import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from review_pipeline.grouping import (INITIAL_CATALOG, JevGroupingClient,
    build_group_request, parse_group_response)
from review_pipeline.jev import MODEL, TOPICS, JevError, JevHTTPError, JevTransportError, JevValidationError


def fake_response(records, catalog=INITIAL_CATALOG):
    request = build_group_request(records, catalog)
    answers = {}
    for i, (key, question) in enumerate(request['questions'].items()):
        choice = 'residual-' + records[i]['topic']
        answers[key] = dict(type='choice', choice=choice, confidence=1,
                           probabilities={k: float(k == choice) for k in question['criteria']})
    return dict(model=MODEL, answers=answers, usage={'input_tokens': 100, 'output_tokens': 10, 'unknown_metric': 12})


class GroupingTests(unittest.TestCase):
    def setUp(self):
        self.records = [dict(review_id='one', review_text='I cannot log in', topic='access', intent='complaint'),
                        dict(review_id='two', review_text='Songs will not play offline', topic='downloads', intent='cancellation')]

    def test_catalog_residuals_cover_all_topics(self):
        for topic in TOPICS:
            self.assertEqual(INITIAL_CATALOG['residual-' + topic]['topic'], topic)

    def test_whitelisted_input_and_per_row_candidates(self):
        records = copy.deepcopy(self.records)
        records[0].update(human_label='SECRET', severity=4, review_rating=1)
        request = build_group_request(records)
        self.assertNotIn('SECRET', json.dumps(request))
        for i, row in enumerate(request['state']['records']):
            self.assertEqual(set(row), {'review_id', 'review_text', 'topic'})
            question = request['questions']['r' + str(i) + '_issue']
            self.assertIn('state.records[' + str(i) + '].review_text', question['instructions'])
            self.assertIn('residual-' + row['topic'], question['criteria'])
            self.assertTrue(all(INITIAL_CATALOG[k]['topic'] == row['topic'] for k in question['criteria']))

    def test_bounds_duplicates_and_non_complaints(self):
        invalid = [[], self.records * 2, [dict(self.records[0], review_text=' ')],
                   [dict(self.records[0], intent='praise')],
                   [dict(self.records[0], review_id=str(i)) for i in range(51)]]
        for records in invalid:
            with self.assertRaises(ValueError):
                build_group_request(records)
        self.assertEqual(len(build_group_request([dict(self.records[0], review_id=str(i)) for i in range(50)])['questions']), 50)
        catalog = copy.deepcopy(INITIAL_CATALOG)
        del catalog['residual-access']
        with self.assertRaisesRegex(ValueError, 'residual-access'):
            build_group_request(self.records, catalog)

    def test_exact_membership_mapping(self):
        actual = parse_group_response(fake_response(self.records), self.records)
        self.assertEqual(actual, [{'issue_id': 'residual-access', 'review_id': 'one'},
                                  {'issue_id': 'residual-downloads', 'review_id': 'two'}])

    def test_malformed_or_cross_topic_answers_reject_entire_batch(self):
        mutations = [lambda r: r['answers'].pop('r1_issue'),
                     lambda r: r['answers'].update(unexpected={}),
                     lambda r: r['answers']['r1_issue'].update(choice='access-login'),
                     lambda r: r['answers']['r1_issue'].update(choice=[]),
                     lambda r: r['answers']['r1_issue'].update(type='noul'),
                     lambda r: r['answers']['r1_issue'].update(confidence=float('nan')),
                     lambda r: r['answers']['r1_issue']['probabilities'].update({'residual-downloads': .2})]
        for mutate in mutations:
            raw = fake_response(self.records)
            mutate(raw)
            with self.assertRaises(JevValidationError) as caught:
                parse_group_response(raw, self.records)
            self.assertIs(caught.exception.raw_response, raw)

    def test_single_attempt_preserves_raw_usage(self):
        raw = fake_response(self.records)
        with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(raw).encode())) as mock:
            rows, actual = JevGroupingClient('test-only-key').evaluate_group(self.records)
        self.assertEqual(actual, raw)
        self.assertEqual(len(rows), 2)
        self.assertEqual(mock.call_count, 1)
        del raw['usage']
        with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(raw).encode())):
            _, actual = JevGroupingClient('test-only-key').evaluate_group(self.records)
        self.assertNotIn('usage', actual)

    def test_errors_accounting_and_model_pin(self):
        with patch('urllib.request.urlopen') as mock:
            with self.assertRaises(JevError):
                JevGroupingClient('').evaluate_group(self.records)
            mock.assert_not_called()
        error = urllib.error.HTTPError('https://api.typesafe.ai', 429, 'rate', {'Retry-After': '2'}, None)
        with patch('urllib.request.urlopen', side_effect=error) as mock:
            with self.assertRaises(JevHTTPError) as caught:
                JevGroupingClient('test-only-key').evaluate_group(self.records)
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.retry_after, '2')
        self.assertEqual(mock.call_count, 1)
        with patch('urllib.request.urlopen', side_effect=TimeoutError):
            with self.assertRaises(JevTransportError) as caught:
                JevGroupingClient('test-only-key').evaluate_group(self.records)
        self.assertTrue(caught.exception.outcome_unknown)
        raw = fake_response(self.records)
        raw['model'] = 'unrequested-version'
        with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(raw).encode())):
            with self.assertRaises(JevValidationError) as caught:
                JevGroupingClient('test-only-key').evaluate_group(self.records)
        self.assertEqual(caught.exception.raw_response, raw)


if __name__ == '__main__':
    unittest.main()
