"""Read-only backend over the saved pipeline database. No model or provider calls."""
import json
import sqlite3
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DB = Path(__file__).resolve().parent.parent / 'data' / 'reviews.sqlite'


def connect():
    db = sqlite3.connect(f'file:{DB}?mode=ro&immutable=1', uri=True)
    db.row_factory = sqlite3.Row
    return db


def query(params):
    kind = params.get('q', ['summary'])[0]
    db = connect()
    try:
        if kind == 'summary':
            stats = {r['key']: json.loads(r['value']) for r in db.execute('SELECT * FROM stats')}
            counts = lambda col: {r[0] or 'none': r[1] for r in db.execute(
                f"SELECT {col}, COUNT(*) FROM reviews WHERE status='completed' GROUP BY {col} ORDER BY 2 DESC")}
            stats['totals'] = dict(db.execute(
                "SELECT COUNT(*) rows, SUM(status='completed') completed, SUM(status='quarantined') quarantined, "
                "SUM(cache_source_id IS NOT NULL) cache_reused, SUM(needs_review) needs_review FROM reviews").fetchone())
            stats['by_topic'], stats['by_intent'] = counts('topic'), counts('intent')
            stats['by_severity'] = counts('severity')
            return stats
        if kind == 'issues':
            return [dict(r) for r in db.execute('SELECT * FROM issues ORDER BY rank')]
        if kind == 'reviews':
            issue = params.get('issue', [None])[0]
            limit = max(1, min(100, int(params.get('limit', ['25'])[0])))
            offset = max(0, int(params.get('offset', ['0'])[0]))
            where, args = ('WHERE issue_id=?', [issue]) if issue else ('', [])
            rows = db.execute(f'SELECT review_id, review_text, rating, review_timestamp, topic, intent, severity, sentiment, '
                              f'needs_review, issue_id, cache_source_id FROM reviews {where} '
                              f'ORDER BY severity DESC, review_id LIMIT ? OFFSET ?', args + [limit, offset])
            return [dict(r) for r in rows]
        raise ValueError('unknown query')
    finally:
        db.close()


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            body, status = json.dumps(query(parse_qs(urlparse(self.path).query)), ensure_ascii=False), 200
        except (ValueError, KeyError) as exc:
            body, status = json.dumps({'error': str(exc)}), 400
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'public, max-age=300')
        self.end_headers()
        self.wfile.write(body.encode('utf-8'))
