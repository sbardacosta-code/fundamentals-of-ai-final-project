import io
import json
import unittest
from unittest.mock import patch

from review_pipeline.compact_jev import CompactJevClient, VARIANTS, build_compact_request
from review_pipeline.jev import (MODEL, SENTIMENT_LEVELS, JevError, JevTransportError,
                                JevValidationError, build_request)


def fake_response(records):
    answers = {}
    for key, question in build_request(records)['questions'].items():
        kind, criteria = question['type'], question['criteria']
        if kind == 'choice':
            choice = next(iter(criteria))
            answer = dict(type=kind, choice=choice, confidence=1,
                          probabilities={k: float(k == choice) for k in criteria})
        elif kind == 'score':
            answer = dict(type=kind, score=2, confidence=1,
                          probabilities={str(i): float(i == 2) for i in range(5)},
                          legend={str(i): v for i, v in enumerate(criteria)})
        else:
            answer = dict(type=kind, noul=.1)
        answers[key] = answer
    return dict(model=MODEL, answers=answers, usage=dict(input_tokens=100, output_tokens=25, extra=7))


class CompactTests(unittest.TestCase):
    def setUp(self):
        self.records = [dict(review_id=str(i), review_text='Synthetic review ' + str(i)) for i in range(5)]

    def test_variants_preserve_keys_scale_targets_and_blind_inputs(self):
        baseline = build_request(self.records)
        for variant in VARIANTS:
            records = [dict(r, topic='SECRET', human_label='GOLDEN') for r in self.records]
            request = build_compact_request(records, role='verify', variant=variant)
            self.assertEqual(set(request['questions']), set(baseline['questions']))
            self.assertNotIn('SECRET', json.dumps(request))
            self.assertNotIn('GOLDEN', json.dumps(request))
            self.assertIn('Independently verify', request['state']['policy'])
            for i in range(5):
                for field in ('topic', 'intent', 'severity', 'sentiment', 'ambiguity'):
                    q = request['questions']['r' + str(i) + '_' + field]
                    self.assertIn('state.records[' + str(i) + '].review_text', q['instructions'])
                self.assertEqual(request['questions']['r' + str(i) + '_sentiment']['criteria'], SENTIMENT_LEVELS)
            self.assertLess(len(json.dumps(request)), len(json.dumps(baseline)))
            if variant in ('shared', 'shared_short', 'shared_rules'):
                self.assertTrue(all(v is None for v in request['questions']['r0_topic']['criteria'].values()))
                self.assertIn('subscription activation failure', request['state']['rubric']['topic']['rule'])

    def test_shared_short_changes_only_instruction_verbosity(self):
        shared = build_compact_request(self.records, variant='shared')
        short = build_compact_request(self.records, variant='shared_short')
        self.assertEqual(shared['state'], short['state'])
        self.assertEqual(shared['model'], short['model'])
        self.assertEqual(set(shared['questions']), set(short['questions']))
        for key, question in short['questions'].items():
            self.assertEqual(question['criteria'], shared['questions'][key]['criteria'])
            self.assertEqual(question['type'], shared['questions'][key]['type'])
            self.assertIn('state.policy', question['instructions'])
            self.assertIn('state.rubric.' + key.split('_', 1)[1], question['instructions'])
        self.assertLess(len(json.dumps(short)), len(json.dumps(shared)))

    def test_shared_rules_retains_complete_rubric_and_adds_inline_precedence(self):
        short = build_compact_request(self.records, variant='shared_short')
        rules = build_compact_request(self.records, variant='shared_rules')
        self.assertEqual(short['state'], rules['state'])
        for key, question in rules['questions'].items():
            self.assertEqual(question['criteria'], short['questions'][key]['criteria'])
            self.assertEqual(question['type'], short['questions'][key]['type'])
            self.assertTrue(question['instructions'].startswith(short['questions'][key]['instructions']))
        self.assertIn('cancellation > complaint > request > praise > unclear', rules['questions']['r0_intent']['instructions'])
        self.assertIn('Negative experience', rules['questions']['r0_intent']['instructions'])
        self.assertIn('Anger/cancellation alone', rules['questions']['r0_severity']['instructions'])
        self.assertIn('ties: first mentioned', rules['questions']['r0_topic']['instructions'])
        self.assertIn('essential context missing', rules['questions']['r0_ambiguity']['instructions'])

    def test_hybrid_uses_exact_selected_questions_without_duplicate_fields(self):
        hybrid = build_compact_request(self.records, variant='hybrid')
        baseline = build_request(self.records)
        shared = build_compact_request(self.records, variant='shared')
        rules = build_compact_request(self.records, variant='shared_rules')
        short = build_compact_request(self.records, variant='shared_short')
        self.assertEqual(len(hybrid['questions']), len(self.records) * 5)
        self.assertNotIn('severity', hybrid['state']['rubric'])
        for i in range(len(self.records)):
            for field, source in (('topic', shared), ('intent', rules), ('severity', baseline),
                                  ('sentiment', short), ('ambiguity', short)):
                key = 'r' + str(i) + '_' + field
                self.assertEqual(hybrid['questions'][key], source['questions'][key])
        expected_state = json.loads(json.dumps(short['state']))
        del expected_state['rubric']['severity']
        self.assertEqual(hybrid['state'], expected_state)

    def test_calibrated_preserves_rubric_and_only_clarifies_intent_severity(self):
        candidate = build_compact_request(self.records, variant='calibrated')
        short = build_compact_request(self.records, variant='shared_short')
        rules = build_compact_request(self.records, variant='shared_rules')
        self.assertEqual(candidate['state'], short['state'])
        self.assertEqual(len(candidate['questions']), len(self.records) * 5)
        for i in range(len(self.records)):
            prefix = 'r' + str(i) + '_'
            self.assertEqual(candidate['questions'][prefix + 'intent'], rules['questions'][prefix + 'intent'])
            for field in ('topic', 'sentiment', 'ambiguity'):
                self.assertEqual(candidate['questions'][prefix + field], short['questions'][prefix + field])
            severity = candidate['questions'][prefix + 'severity']
            self.assertEqual(severity['criteria'], short['questions'][prefix + 'severity']['criteria'])
            self.assertTrue(severity['instructions'].startswith(short['questions'][prefix + 'severity']['instructions']))
            self.assertIn('demonstrated functional impact', severity['instructions'])
            self.assertIn('Preserve severity 1 and 5', severity['instructions'])

    def test_five_ten_and_hard_limit(self):
        for count in (5, 10):
            rows = [dict(review_id=str(i), review_text='fixture') for i in range(count)]
            for variant in VARIANTS:
                self.assertEqual(len(build_compact_request(rows, variant=variant)['questions']), count * 5)
        with self.assertRaises(ValueError):
            build_compact_request([dict(review_id=str(i), review_text='fixture') for i in range(51)])
        with self.assertRaises(ValueError):
            build_compact_request(self.records, variant='unknown')

    def test_baseline_parser_round_trip_and_raw_usage(self):
        for variant in VARIANTS:
            raw = fake_response(self.records)
            with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(raw).encode())) as mock:
                parsed, actual = CompactJevClient('test-only-key', variant=variant).evaluate(self.records)
            self.assertEqual([r['review_id'] for r in parsed], [r['review_id'] for r in self.records])
            self.assertEqual(actual, raw)
            self.assertEqual(mock.call_count, 1)

    def test_missing_key_unknown_outcome_and_invalid_model(self):
        with patch('urllib.request.urlopen') as mock:
            with self.assertRaises(JevError):
                CompactJevClient('').evaluate(self.records)
            mock.assert_not_called()
        with patch('urllib.request.urlopen', side_effect=TimeoutError):
            with self.assertRaises(JevTransportError) as caught:
                CompactJevClient('test-only-key').evaluate(self.records)
        self.assertTrue(caught.exception.outcome_unknown)
        raw = fake_response(self.records)
        raw['model'] = 'other'
        with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(raw).encode())):
            with self.assertRaises(JevValidationError) as caught:
                CompactJevClient('test-only-key').evaluate(self.records)
        self.assertEqual(caught.exception.raw_response, raw)


if __name__ == '__main__':
    unittest.main()
