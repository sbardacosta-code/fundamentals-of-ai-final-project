import csv
import json
from pathlib import Path
import tempfile
import unittest
from review_pipeline.labeling import LabelStore, validate_label
from review_pipeline.evaluation import evaluate


def label(key='one'):
    return {'review_id': key, 'review_text': 'Music cannot play.', 'label_source': 'human',
            'topic': 'playback', 'intent': 'complaint', 'severity': 4, 'sentiment': -0.8,
            'entities': [], 'evidence_quote': 'cannot play', 'needs_review': False, 'revision': 0}


class HumanEvaluationTests(unittest.TestCase):
    def test_reject_bad_labels(self):
        for field, value in [('topic', 'bad'), ('intent', 'bad'), ('severity', True), ('severity', 6),
                             ('sentiment', float('nan')), ('sentiment', True), ('sentiment', 2),
                             ('entities', 'Spotify'), ('evidence_quote', 'invented'),
                             ('evidence_quote', ''), ('needs_review', 'false')]:
            candidate = label()
            candidate[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                validate_label(candidate, candidate['review_text'])

    def test_atomic_save_and_stale_revision(self):
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / 'source.csv', Path(folder) / 'labels.jsonl'
            with source.open('w', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=['review_id', 'review_text', 'topic'])
                writer.writeheader()
                writer.writerow({'review_id': 'one', 'review_text': label()['review_text'], 'topic': 'secret-prediction'})
            store = LabelStore(source, output)
            self.assertNotIn('topic', store.snapshot()['reviews'][0])
            self.assertEqual(store.snapshot()['labels'], {})
            saved = store.save(label())
            self.assertEqual(saved['revision'], 1)
            before = output.read_bytes()
            with self.assertRaises(ValueError):
                store.save(label())
            self.assertEqual(output.read_bytes(), before)
            saved['severity'] = 3
            self.assertEqual(store.save(saved)['revision'], 2)
            self.assertEqual(len(output.read_text().splitlines()), 1)

    def test_missing_predictions_stay_in_denominator(self):
        with tempfile.TemporaryDirectory() as folder:
            golden, predictions, output = [Path(folder) / x for x in ('gold.jsonl', 'pred.jsonl', 'report.json')]
            golden.write_text('\n'.join(json.dumps(label(k)) for k in ('one', 'two', 'three')))
            predicted = label()
            predicted.update(severity=3, sentiment=-0.5)
            invalid = label('two')
            invalid['evidence_quote'] = 'not there'
            predictions.write_text(json.dumps(predicted) + '\n' + json.dumps(invalid))
            report = evaluate(golden, predictions, output)
            self.assertEqual(report['topic_accuracy'], 1 / 3)
            self.assertEqual(report['severity_mae'], 1)
            self.assertAlmostEqual(report['sentiment_mae'], .3)
            self.assertEqual(report['missing_prediction_ids'], ['three'])
            self.assertEqual(report['invalid_predictions'][0]['review_id'], 'two')
            self.assertEqual(report['mae_denominator'], 1)
            self.assertFalse(report['golden_complete'])
            self.assertEqual(json.loads(output.read_text()), report)

    def test_refuse_duplicate_or_nonhuman_answers(self):
        with tempfile.TemporaryDirectory() as folder:
            golden, predictions, output = [Path(folder) / x for x in ('gold.jsonl', 'pred.jsonl', 'report.json')]
            predictions.write_text('')
            row = label()
            row['label_source'] = 'model'
            golden.write_text(json.dumps(row))
            with self.assertRaises(ValueError):
                evaluate(golden, predictions, output)
            golden.write_text(json.dumps(label()) + '\n' + json.dumps(label()))
            with self.assertRaises(ValueError):
                evaluate(golden, predictions, output)


if __name__ == '__main__':
    unittest.main()
