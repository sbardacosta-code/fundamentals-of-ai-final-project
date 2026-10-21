"""Transactional source preservation, queue, result cache and request accounting."""
import csv
import hashlib
import json
import sqlite3
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

FIELDS = ('review_id', 'review_text', 'review_rating', 'review_likes', 'app_version', 'review_timestamp')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def file_sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def atomic_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False, allow_nan=False) + '\n', encoding='utf-8')
    tmp.replace(path)


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS datasets(id TEXT PRIMARY KEY, path TEXT, profile TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS reviews(
          review_id TEXT PRIMARY KEY, source_sha256 TEXT NOT NULL,
          text_hash TEXT NOT NULL, source_json TEXT NOT NULL, empty INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS reviews_text ON reviews(text_hash);
        CREATE TABLE IF NOT EXISTS dataset_rows(
          dataset_id TEXT NOT NULL REFERENCES datasets(id),
          review_id TEXT NOT NULL REFERENCES reviews(review_id), ordinal INTEGER NOT NULL,
          PRIMARY KEY(dataset_id, review_id));
        CREATE TABLE IF NOT EXISTS configs(id TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs(
          config_id TEXT NOT NULL REFERENCES configs(id), text_hash TEXT NOT NULL,
          source_id TEXT NOT NULL REFERENCES reviews(review_id),
          state TEXT NOT NULL DEFAULT 'pending', attempt_count INTEGER NOT NULL DEFAULT 0,
          owner TEXT, lease_until REAL, result_json TEXT, reason TEXT,
          PRIMARY KEY(config_id,text_hash));
        CREATE INDEX IF NOT EXISTS jobs_state ON jobs(config_id,state);
        CREATE TABLE IF NOT EXISTS runs(
          id TEXT PRIMARY KEY, budget REAL NOT NULL CHECK(budget>=0),
          spent REAL NOT NULL DEFAULT 0, reserved REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS calls(
          id TEXT PRIMARY KEY, run_id TEXT REFERENCES runs(id), config_id TEXT NOT NULL,
          owner TEXT NOT NULL, reservation REAL NOT NULL, outcome TEXT NOT NULL,
          payload TEXT NOT NULL, created REAL NOT NULL);
        ''')

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def ingest(self, path):
        """Stream CSV; reject changed/duplicate IDs. One atomic dataset import."""
        dataset_id = file_sha(path)
        prior = self.db.execute('SELECT profile FROM datasets WHERE id=?', (dataset_id,)).fetchone()
        if prior:
            return json.loads(prior[0])
        counts = Counter(records=0, duplicate_review_ids=0, empty_review_text=0, missing_app_version=0)
        months, ratings = Counter(), Counter()
        parsed = hashlib.sha256()
        first = last = None
        with self.transaction():
            self.db.execute('INSERT INTO datasets VALUES(?,?,?)', (dataset_id,str(Path(path).resolve()),'{}'))
            with open(path, encoding='utf-8-sig', newline='') as stream:
                reader = csv.DictReader(stream, strict=True)
                if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames) or not set(FIELDS) <= set(reader.fieldnames):
                    raise ValueError('CSV must have unique headers and all six source fields')
                for ordinal, row in enumerate(reader):
                    if None in row or any(row[k] is None for k in FIELDS) or not row['review_id']:
                        raise ValueError(f'Malformed row {ordinal + 2}')
                    source = {k:row[k] for k in FIELDS}
                    rid = source['review_id']
                    sha = digest(canonical([source[k] for k in FIELDS]))
                    th = digest(source['review_text'])
                    empty = not source['review_text'].strip()
                    self.db.execute('INSERT OR IGNORE INTO reviews VALUES(?,?,?,?,?)', (rid,sha,th,canonical(source),empty))
                    actual = self.db.execute('SELECT source_sha256 FROM reviews WHERE review_id=?',(rid,)).fetchone()[0]
                    if actual != sha:
                        raise ValueError(f'Source ID has different original values: {rid}')
                    try:
                        self.db.execute('INSERT INTO dataset_rows VALUES(?,?,?)',(dataset_id,rid,ordinal))
                    except sqlite3.IntegrityError as exc:
                        raise ValueError(f'Duplicate source ID: {rid}') from exc
                    parsed.update(bytes.fromhex(sha))
                    counts['records'] += 1
                    counts['empty_review_text'] += empty
                    counts['missing_app_version'] += not source['app_version'].strip()
                    months[source['review_timestamp'][:7]] += 1
                    ratings[source['review_rating']] += 1
                    timestamp = source['review_timestamp']
                    first = min(first, timestamp) if first is not None else timestamp
                    last = max(last, timestamp) if last is not None else timestamp
            unique = self.db.execute('SELECT COUNT(DISTINCT r.text_hash) FROM reviews r JOIN dataset_rows d USING(review_id) WHERE d.dataset_id=? AND r.empty=0',(dataset_id,)).fetchone()[0]
            profile = dict(file_sha256=dataset_id,parsed_rows_sha256=parsed.hexdigest(),counts=dict(counts),
                reviews_by_month=dict(months),reviews_by_rating=dict(ratings),first_review=first,last_review=last,
                unique_nonempty_texts=unique, exact_text_reuse=counts['records']-counts['empty_review_text']-unique)
            self.db.execute('UPDATE datasets SET profile=? WHERE id=?',(canonical(profile),dataset_id))
        return profile

    def prepare(self, dataset_id, config):
        if config.get('role') not in ('enrich','verify','group'):
            raise ValueError('Queue configuration requires enrich, verify or group role')
        cid = digest(canonical(config))
        with self.transaction():
            if not self.db.execute('SELECT 1 FROM datasets WHERE id=?',(dataset_id,)).fetchone():
                raise ValueError('Unknown dataset')
            self.db.execute('INSERT OR IGNORE INTO configs VALUES(?,?)',(cid,canonical(config)))
            # Insertion order selects a direct original, never a chain of aliases.
            self.db.execute('''INSERT OR IGNORE INTO jobs(config_id,text_hash,source_id)
                SELECT ?,r.text_hash,r.review_id FROM dataset_rows d JOIN reviews r USING(review_id)
                WHERE d.dataset_id=? AND r.empty=0 ORDER BY d.ordinal''',(cid,dataset_id))
        return cid

    def claim(self, config_id, owner, batch_size=10, lease_seconds=300):
        if not 1 <= batch_size <= 50 or lease_seconds <= 0:
            raise ValueError('Batch size must be 1..50 and lease positive')
        with self.transaction():
            # Only un-dispatched claims can automatically expire. A request with an
            # unknown provider outcome remains uncertain, avoiding an automatic rebill.
            self.db.execute("UPDATE jobs SET state='pending',owner=NULL,lease_until=NULL WHERE config_id=? AND state='claimed' AND lease_until<?",(config_id,time.time()))
            jobs = self.db.execute("SELECT j.text_hash,j.source_id,r.source_json FROM jobs j JOIN reviews r ON r.review_id=j.source_id WHERE j.config_id=? AND j.state='pending' ORDER BY j.source_id LIMIT ?",(config_id,batch_size)).fetchall()
            for job in jobs:
                self.db.execute("UPDATE jobs SET state='claimed',owner=?,lease_until=? WHERE config_id=? AND text_hash=?",(owner,time.time()+lease_seconds,config_id,job['text_hash']))
        return [dict(json.loads(j['source_json']), text_hash=j['text_hash']) for j in jobs]

    def create_run(self, budget):
        if not isinstance(budget,(int,float)) or not 0 < budget < float('inf'):
            raise ValueError('A finite positive budget is required')
        rid = uuid.uuid4().hex
        self.db.execute('INSERT INTO runs(id,budget) VALUES(?,?)',(rid,budget))
        return rid

    def dispatch(self, run_id, config_id, owner, records, reservation, phase='initial'):
        if not records or len(records)>50 or not 0 < reservation < float('inf'):
            raise ValueError('A nonempty bounded batch and positive reservation are required')
        if len({r['text_hash'] for r in records}) != len(records):
            raise ValueError('Duplicate tasks in dispatch')
        call_id=uuid.uuid4().hex
        with self.transaction():
            ledger=self.db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
            if not ledger or ledger['spent']+ledger['reserved']+reservation>ledger['budget']:
                raise ValueError('Spending cap would be exceeded')
            for row in records:
                stored=self.db.execute('SELECT r.source_json FROM jobs j JOIN reviews r ON r.review_id=j.source_id WHERE j.config_id=? AND j.text_hash=?',(config_id,row['text_hash'])).fetchone()
                if not stored or any(row.get(k)!=v for k,v in json.loads(stored[0]).items()):
                    raise ValueError('Dispatch must preserve the claimed original review')
                n=self.db.execute("UPDATE jobs SET state='in_flight',attempt_count=attempt_count+1 WHERE config_id=? AND text_hash=? AND state='claimed' AND owner=? AND lease_until>?",(config_id,row['text_hash'],owner,time.time())).rowcount
                if n!=1:
                    raise ValueError('Lost or invalid task ownership')
            config=json.loads(self.db.execute('SELECT value FROM configs WHERE id=?',(config_id,)).fetchone()[0])
            payload=dict(request_id=call_id,run_id=run_id,role=config['role'],model=config['model'],phase=phase,run_phase=phase,label_config=config_id,review_ids=[r['review_id'] for r in records],started_at=datetime.now(timezone.utc).isoformat(),outcome='in_flight')
            self.db.execute('INSERT INTO calls VALUES(?,?,?,?,?,?,?,?)',(call_id,run_id,config_id,owner,reservation,'in_flight',canonical(payload),time.time()))
            self.db.execute('UPDATE runs SET reserved=reserved+? WHERE id=?',(reservation,run_id))
        return call_id

    def finish(self, call_id, predictions=None, usage=None, cost=None, error=None, retry=False, raw_response=None):
        """Persist handoff and call together. Unknown charges retain reservations."""
        if cost is not None and (not isinstance(cost,(int,float)) or not 0<=cost<float('inf')):
            raise ValueError('Cost must be finite and nonnegative or unknown')
        with self.transaction():
            call=self.db.execute('SELECT * FROM calls WHERE id=?',(call_id,)).fetchone()
            if not call or call['outcome']!='in_flight':
                raise ValueError('Call is missing or already finalized')
            log=json.loads(call['payload']); ids=log['review_ids']
            if error is None:
                if not isinstance(predictions,list) or len(predictions)!=len(ids) or {r.get('review_id') for r in predictions}!=set(ids):
                    raise ValueError('Predictions must match every dispatched ID exactly')
                for record in predictions:
                    source=json.loads(self.db.execute('SELECT source_json FROM reviews WHERE review_id=?',(record['review_id'],)).fetchone()[0])
                    validate_prediction(record,source['review_text'])
                for record in predictions:
                    n=self.db.execute("UPDATE jobs SET state='completed',result_json=?,lease_until=NULL WHERE config_id=? AND source_id=? AND owner=? AND state='in_flight'",(canonical(record),call['config_id'],record['review_id'],call['owner'])).rowcount
                    if n!=1: raise ValueError('Completion lost task ownership')
                outcome='succeeded'
            else:
                # Unknown charge or timeout cannot be safely retried automatically.
                state='uncertain' if cost is None else ('pending' if retry else 'quarantined')
                for rid in ids:
                    attempt=self.db.execute('SELECT attempt_count FROM jobs WHERE config_id=? AND source_id=?',(call['config_id'],rid)).fetchone()[0]
                    bounded_state='quarantined' if state=='pending' and attempt>=2 else state
                    n=self.db.execute("UPDATE jobs SET state=?,reason=?,owner=NULL,lease_until=NULL WHERE config_id=? AND source_id=? AND owner=? AND state='in_flight'",(bounded_state,str(error),call['config_id'],rid,call['owner'])).rowcount
                    if n!=1: raise ValueError('Failure finalization lost task ownership')
                outcome='failed'
            if cost is not None:
                self.db.execute('UPDATE runs SET reserved=reserved-?,spent=spent+? WHERE id=?',(call['reservation'],cost,call['run_id']))
            log.update(outcome=outcome,ended_at=datetime.now(timezone.utc).isoformat(),cost_usd=cost,usage_available=usage is not None)
            if usage:
                for key in ('input_tokens','output_tokens','cached_input_tokens'):
                    if key in usage: log[key]=usage[key]
            if raw_response is not None: log['raw_response']=raw_response
            if error is not None: log['error']=str(error)
            self.db.execute('UPDATE calls SET outcome=?,payload=? WHERE id=?',(outcome,canonical(log),call_id))

    def status(self, config_id=None):
        where=' WHERE config_id=?' if config_id else ''
        states={r[0]:r[1] for r in self.db.execute('SELECT state,COUNT(*) FROM jobs'+where+' GROUP BY state', (config_id,) if config_id else ())}
        return dict(source_rows=self.db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0],jobs=states,runs=[dict(r) for r in self.db.execute('SELECT * FROM runs')])

    def records(self,dataset_id,config_id):
        for row in self.db.execute('''SELECT r.*,j.state,j.result_json,j.source_id,j.reason,j.attempt_count
          FROM dataset_rows d JOIN reviews r USING(review_id)
          LEFT JOIN jobs j ON j.text_hash=r.text_hash AND j.config_id=?
          WHERE d.dataset_id=? ORDER BY d.ordinal''',(config_id,dataset_id)):
            base=dict(review_id=row['review_id'],source_sha256=row['source_sha256'],label_config=config_id)
            if row['empty']:
                yield dict(base,status='quarantined',reason='empty_review_text'); continue
            if row['state']=='completed':
                result=json.loads(row['result_json']); result.update(base,status='completed')
                if row['source_id']!=row['review_id']:
                    if not self.db.execute('SELECT 1 FROM dataset_rows WHERE dataset_id=? AND review_id=?',(dataset_id,row['source_id'])).fetchone():
                        raise ValueError('Cache original is outside this dataset. Use a separate database for isolated pilot/subset runs; never fabricate original-call provenance.')
                    result['cache_source_id']=row['source_id']
                yield result
            else:
                yield dict(base,status=row['state'] or 'pending',reason=row['reason'],attempt_count=row['attempt_count'] or 0)

    def snapshot(self,dataset_id,config_id):
        completed=[r['review_id'] for r in self.records(dataset_id,config_id) if r['status']=='completed']
        return dict(completed_ids=sorted(completed),created_at=time.time(),config_id=config_id,dataset_id=dataset_id)


def validate_prediction(row,text):
    if row.get('topic') not in ('access','usability','playback','downloads','catalog','billing','support','other'):
        raise ValueError('Invalid topic')
    if row.get('intent') not in ('cancellation','complaint','request','praise','unclear'):
        raise ValueError('Invalid intent')
    if type(row.get('severity')) is not int or not 1<=row['severity']<=5:
        raise ValueError('Invalid severity')
    s=row.get('sentiment')
    if isinstance(s,bool) or not isinstance(s,(float,int)) or not -1<=s<=1:
        raise ValueError('Invalid sentiment')
    if not isinstance(row.get('evidence_quote'),str) or not row['evidence_quote'] or row['evidence_quote'] not in text:
        raise ValueError('Evidence quote is not an exact nonempty substring')
    if not isinstance(row.get('entities'),list) or not all(isinstance(v,str) for v in row['entities']):
        raise ValueError('Invalid entities')
    if any(not v or v.casefold() not in text.casefold() for v in row['entities']):
        raise ValueError('Unsupported entity: only explicit source terms may be exported')
    if type(row.get('needs_review')) is not bool:
        raise ValueError('Invalid needs_review')
