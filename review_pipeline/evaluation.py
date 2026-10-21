"""Offline evaluation; human answers are never passed to a model."""
import json
import os
from pathlib import Path
import tempfile
from .labeling import read_jsonl, validate_label


def evaluate(golden_path, predictions_path, out_path):
    for source in (golden_path, predictions_path):
        if not Path(source).is_file():
            raise FileNotFoundError(source)
    golden = read_jsonl(golden_path)
    predictions = read_jsonl(predictions_path)
    if not golden:
        raise ValueError('No human labels to evaluate.')
    if Path(out_path).resolve() in (Path(golden_path).resolve(), Path(predictions_path).resolve()):
        raise ValueError('Report output must differ from input paths.')
    exact = {'topic': 0, 'intent': 0, 'severity': 0}
    errors = {'severity': [], 'sentiment': []}
    disagreements, missing, invalid = [], [], []
    quote_valid, entities_exact, needs_review_exact, sentiment_within = 0, 0, 0, 0
    for key, expected in golden.items():
        if expected.get('label_source') != 'human':
            raise ValueError('Golden records must explicitly identify label_source=human.')
        text = expected.get('review_text')
        if not isinstance(text, str):
            raise ValueError('Golden records require original review_text for evidence validation.')
        validate_label(expected, text)
        actual = predictions.get(key)
        if actual is None or actual.get('status') == 'quarantined':
            missing.append(key)
            disagreements.append({'review_id': key, 'reason': 'missing_or_quarantined'})
            continue
        try:
            validate_label(actual, text)
        except (ValueError, TypeError) as exc:
            invalid.append({'review_id': key, 'reason': str(exc)})
            disagreements.append({'review_id': key, 'reason': 'invalid_prediction', 'detail': str(exc)})
            continue
        differences = {}
        for field in exact:
            if expected[field] == actual[field]:
                exact[field] += 1
            else:
                differences[field] = {'human': expected[field], 'predicted': actual[field]}
        for field in errors:
            error = abs(expected[field] - actual[field])
            errors[field].append(error)
            if error and field not in differences:
                differences[field] = {'human': expected[field], 'predicted': actual[field]}
        quote_valid += 1
        entities_exact += set(expected['entities']) == set(actual['entities'])
        needs_review_exact += expected['needs_review'] == actual['needs_review']
        sentiment_within += abs(expected['sentiment'] - actual['sentiment']) <= 0.2 + 1e-12
        for field in ('entities', 'needs_review'):
            if expected[field] != actual[field]:
                differences[field] = {'human': expected[field], 'predicted': actual[field]}
        if differences:
            disagreements.append({'review_id': key, 'differences': differences})
    total = len(golden)
    valid = len(errors['severity'])
    report = {
        'golden_count': total, 'expected_golden_count': 50, 'golden_complete': total == 50,
        'valid_prediction_count': valid, 'coverage': valid / total,
        'accuracy_denominator': total,
        'topic_accuracy': exact['topic'] / total, 'intent_accuracy': exact['intent'] / total,
        'severity_exact_accuracy': exact['severity'] / total,
        'severity_mae': sum(errors['severity']) / valid if valid else None,
        'sentiment_mae': sum(errors['sentiment']) / valid if valid else None,
        'mae_denominator': valid,
        'sentiment_tolerance': 0.2, 'sentiment_within_tolerance_fraction': sentiment_within / total,
        'human_ambiguity_count': sum(x['needs_review'] for x in golden.values()),
        'quote_valid_count': quote_valid, 'quote_support': 'requires human audit; exact substring alone does not establish support',
        'entities_exact_fraction': entities_exact / total, 'needs_review_exact_fraction': needs_review_exact / total,
        'missing_prediction_ids': missing, 'invalid_predictions': invalid,
        'disagreements': disagreements,
        'notes': 'Missing/quarantined/invalid predictions count as incorrect for agreement. MAE uses valid predictions only; inspect coverage alongside MAE. Sentiment tolerance is a declared project choice. Do not tune on these holdout labels.',
    }
    output = Path(out_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=output.name + '.', dir=output.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False, allow_nan=False)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return report
