"""Build dashboard/data/reviews.sqlite from saved pipeline outputs. Offline; no API calls.

Run from the repository root: python3 dashboard/build_db.py
Requires the local scale store (artifacts/scale/scale.sqlite) for review text.
"""
import csv, json, sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FINAL = ROOT / 'artifacts/scale/sample_100000'
OUT = ROOT / 'dashboard/data/reviews.sqlite'


def main():
    OUT.unlink(missing_ok=True)
    db = sqlite3.connect(OUT)
    db.executescript('''
    CREATE TABLE reviews(review_id TEXT PRIMARY KEY, review_text TEXT, rating TEXT, review_timestamp TEXT,
      status TEXT, topic TEXT, intent TEXT, severity INTEGER, sentiment REAL, needs_review INTEGER,
      issue_id TEXT, cache_source_id TEXT);
    CREATE INDEX reviews_issue ON reviews(issue_id, severity);
    CREATE TABLE issues(rank INTEGER, issue_id TEXT PRIMARY KEY, complaint_count INTEGER, severity_sum INTEGER,
      mean_severity TEXT, priority_score INTEGER);
    CREATE TABLE stats(key TEXT PRIMARY KEY, value TEXT);''')
    store = sqlite3.connect(ROOT / 'artifacts/scale/scale.sqlite')
    manifest = json.loads((FINAL / 'manifest.json').read_text())
    sources = {rid: json.loads(src) for rid, src in store.execute(
        'SELECT r.review_id, r.source_json FROM reviews r JOIN dataset_rows d USING(review_id) WHERE d.dataset_id=?',
        (manifest['identity']['dataset_id'],))}
    membership = {r['review_id']: r['issue_id'] for r in csv.DictReader(open(FINAL / 'membership.csv'))}
    rows = []
    for line in open(FINAL / 'records.jsonl'):
        r = json.loads(line); s = sources[r['review_id']]
        rows.append((r['review_id'], s['review_text'], s['review_rating'], s['review_timestamp'], r['status'],
                     r.get('topic'), r.get('intent'), r.get('severity'), r.get('sentiment'),
                     int(bool(r.get('needs_review'))), membership.get(r['review_id']), r.get('cache_source_id')))
    db.executemany('INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', rows)
    db.executemany('INSERT INTO issues VALUES(?,?,?,?,?,?)', [
        (int(r['rank']), r['issue_id'], int(r['complaint_count']), int(r['severity_sum']), r['mean_severity'], int(r['priority_score']))
        for r in csv.DictReader(open(FINAL / 'ranking.csv'))])
    checkpoints = {}
    for name in ('checkpoint_500', 'analysis_10000', 'sample_100000'):
        run = json.loads((ROOT / 'artifacts/scale' / name / 'run.json').read_text())
        comparison = json.loads((ROOT / 'artifacts/scale' / name / 'verification_comparison.json').read_text())
        attempts = run['attempts']
        checkpoints[name] = dict(rows=sum(run['status_counts'].values()), status=run['status_counts'],
            cost_by_role_usd=run['cost_by_role_usd'], spent_usd=run['ledger']['spent'], cap_usd=run['budget_usd'],
            workers=attempts[-1]['workers'], batch_size=attempts[-1]['batch_size'],
            elapsed_seconds=sum(a['elapsed_seconds'] for a in attempts), cache_reused_rows=run['cache_reused_rows'],
            calls=run['calls_by_role_outcome'], verified=len(comparison),
            verifier_agreement=sum(not c['disagreements'] for c in comparison))
        memo_run = ROOT / 'artifacts/scale' / name / 'memo_run.json'
        if memo_run.exists():
            checkpoints[name]['memo_spent_usd'] = json.loads(memo_run.read_text())['ledger']['spent']
    stats = dict(checkpoints=checkpoints, memo=json.loads((FINAL / 'memo.json').read_text()),
                 pilot=dict(rows=100, cost_usd=0.010991572, workers=1),
                 models=dict(classifier='Jev jev-1.13.0 (TypeSafe)', memo='Claude Haiku 5.5 (claude-haiku-5-5)'),
                 scope='Deterministic 100,000-review sample of the 660,622-row corpus (instructor seed and ordering).')
    # Real reviews used by the dashboard's animated walkthrough (fixed IDs, verified first example).
    picks = [('23dba90f-17d7-4ee7-9b6f-1603a2f81335', True), ('10f09c93-34ef-4a3b-8ee2-97d7c00d44e0', False),
             ('57c09fe7-3f31-42ab-8bc3-016229670470', False)]
    verified = {json.loads(l)['review_id']: json.loads(l) for l in open(FINAL / 'verification.jsonl')}
    examples = []
    for rid, _ in picks:
        r = dict(zip(('review_id', 'review_text', 'topic', 'intent', 'severity', 'sentiment', 'issue_id', 'cache_source_id'),
                     db.execute('SELECT review_id, review_text, topic, intent, severity, sentiment, issue_id, cache_source_id '
                                'FROM reviews WHERE review_id=?', (rid,)).fetchone()))
        v = verified.get(rid)
        r['verified'] = bool(v)
        r['verifier_agrees'] = bool(v) and all(v[k] == r[k] for k in ('topic', 'intent', 'severity'))
        examples.append(r)
    stats['examples'] = examples
    db.executemany('INSERT INTO stats VALUES(?,?)', [(k, json.dumps(v)) for k, v in stats.items()])
    db.commit(); db.execute('VACUUM'); db.close()
    print(OUT, OUT.stat().st_size)


if __name__ == '__main__':
    main()
