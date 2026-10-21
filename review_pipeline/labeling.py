"""Local, blind human labeling. No model calls or generated answers."""
import csv
import fcntl
import json
import math
import os
from pathlib import Path
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOPICS = ('access', 'usability', 'playback', 'downloads', 'catalog', 'billing', 'support', 'other')
INTENTS = ('cancellation', 'complaint', 'request', 'praise', 'unclear')
FIELDS = ('topic', 'intent', 'severity', 'sentiment', 'entities', 'evidence_quote', 'needs_review')


def validate_label(label, text):
    if label.get('topic') not in TOPICS or label.get('intent') not in INTENTS:
        raise ValueError('Choose a valid topic and intent.')
    severity = label.get('severity')
    if type(severity) is not int or not 1 <= severity <= 5:
        raise ValueError('Severity must be an integer from 1 to 5.')
    sentiment = label.get('sentiment')
    if type(sentiment) not in (int, float) or not math.isfinite(sentiment) or not -1 <= sentiment <= 1:
        raise ValueError('Sentiment must be a finite number from -1 to 1.')
    entities = label.get('entities')
    if not isinstance(entities, list) or any(not isinstance(x, str) or not x.strip() for x in entities):
        raise ValueError('Entities must be a list of nonempty strings.')
    quote = label.get('evidence_quote')
    if not isinstance(quote, str) or not quote or quote not in text:
        raise ValueError('Evidence must be a nonempty exact substring of the review.')
    if type(label.get('needs_review')) is not bool:
        raise ValueError('Needs review must be true or false.')
    return {key: label[key] for key in FIELDS}


def read_jsonl(path):
    rows = {}
    if Path(path).exists():
        with open(path, encoding='utf-8') as handle:
            for line in handle:
                if line.strip():
                    row = json.loads(line)
                    key = row['review_id']
                    if key in rows:
                        raise ValueError(f'Duplicate review ID: {key}')
                    rows[key] = row
    return rows


def atomic_jsonl(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class LabelStore:
    def __init__(self, input_path, output_path):
        with open(input_path, newline='', encoding='utf-8-sig') as handle:
            reader = csv.DictReader(handle)
            if not {'review_id', 'review_text'} <= set(reader.fieldnames or []):
                raise ValueError('CSV requires review_id and review_text.')
            # Deliberately ignore any preexisting label/prediction columns.
            fields = ('review_id', 'review_text', 'review_rating', 'review_likes', 'app_version', 'review_timestamp')
            self.rows = [{key: row.get(key, '') for key in fields} for row in reader]
        self.by_id = {r['review_id']: r for r in self.rows}
        if len(self.by_id) != len(self.rows) or '' in self.by_id:
            raise ValueError('Source review IDs must be nonempty and unique.')
        self.output = Path(output_path)
        if self.output.resolve() == Path(input_path).resolve():
            raise ValueError('Output must differ from input.')
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self._check_existing(read_jsonl(self.output))

    def _check_existing(self, labels):
        for key, label in labels.items():
            if key not in self.by_id or label.get('review_text') != self.by_id[key]['review_text']:
                raise ValueError('Existing labels belong to different source data; use a separate output path.')
            validate_label(label, self.by_id[key]['review_text'])

    def snapshot(self):
        with self.lock:
            labels = read_jsonl(self.output)
            self._check_existing(labels)
            return {'reviews': self.rows, 'labels': labels}

    def save(self, payload):
        key = payload.get('review_id')
        if not isinstance(key, str) or key not in self.by_id:
            raise ValueError('Unknown review ID.')
        values = validate_label(payload, self.by_id[key]['review_text'])
        with self.lock, open(str(self.output) + '.lock', 'a') as lockfile:
            fcntl.flock(lockfile, fcntl.LOCK_EX)
            labels = read_jsonl(self.output)
            self._check_existing(labels)
            revision = labels.get(key, {}).get('revision', 0)
            if payload.get('revision') != revision:
                raise ValueError('This label changed in another tab. Reload before saving.')
            row = dict(values, review_id=key, review_text=self.by_id[key]['review_text'],
                       revision=revision + 1, label_source='human')
            labels[key] = row
            atomic_jsonl(self.output, labels.values())
            return row


HTML = r'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Human review labels</title>
<style>body{max-width:950px;margin:35px auto;padding:0 20px;font:16px system-ui;color:#172c37;background:#f5f8fa}article,details{background:white;padding:20px;margin:16px 0;border:1px solid #ccd8de;border-radius:10px}label{display:block;margin:16px 0}select,input,textarea,button{font:inherit;padding:8px}textarea{width:95%}blockquote{white-space:pre-wrap;margin:16px 0;font-size:19px}button{cursor:pointer}#status{min-height:24px}small{color:#49616e}</style>
<h1>Human review labels</h1><p>Label the text yourself before viewing model results. No predictions are shown. Save each review; return later to continue.</p>
<details><summary>Labeling rubric — read before starting</summary><p>Classify text, not stars. Select the highest-supported-severity problem, first mentioned on a tie. Positive reviews: first specific praised feature.</p>
<p><b>Topics:</b> access: login/signup/password; usability: navigation, controls, layout, queues/playlists, ads; playback: failures/crashes/lag/connections/audio/resource use; downloads: downloads/offline/saved music; catalog: missing songs, discovery/search/recommendations/lyrics; billing: prices/charges/subscriptions/paywalls/premium entitlement; support: contact/response; other: general or unrelated content.</p>
<p>A paid-plan mention alone is not billing: music crashing for a paying customer is playback.</p><p><b>Intent precedence:</b> cancellation (explicit leaving/uninstalling/cancelling/threatening departure) → complaint (negative experience, even mixed) → request (desired change without failure) → praise → unclear. Bare boycott slogans without product complaint or personal departure are unclear.</p>
<p><b>Severity:</b> 1: no problem, pure request, praise or neutral; 2: annoyance/generic criticism, no functional loss; 3: degraded function, some use remains; 4: clearly blocked core task; 5: explicit serious financial/privacy/data harm. Anger, cancellation or stars alone do not raise severity.</p><p><b>Sentiment:</b> −1 strongly negative, 0 neutral, +1 strongly positive. Use intermediate values as appropriate. Flag missing context with needs review. Copy an exact supporting quote. Entities are explicitly mentioned names/products.</p></details>
<p id="progress"></p><button id="prev">Previous</button> <button id="next">Next</button> <button id="unlabeled">Next unlabeled</button>
<article><small id="identity"></small><blockquote id="text"></blockquote><details><summary>Original metadata (do not classify from stars)</summary><pre id="metadata"></pre></details>
<form id="form"><label>Topic <select id="topic" required></select></label><label>Intent <select id="intent" required></select></label><label>Severity <select id="severity" required></select></label><label>Sentiment <input id="sentiment" type="number" min="-1" max="1" step="any" required></label><label>Entities (one per line)<textarea id="entities" rows="2"></textarea></label><label>Exact evidence quote<textarea id="evidence_quote" rows="3" required></textarea></label><label><input id="needs_review" type="checkbox"> Needs review / ambiguous</label><button>Save human label</button></form></article><p id="status" role="status"></p>
<script>
const el=id=>document.getElementById(id);let state,index=0,dirty=false;
for(const [id,values] of Object.entries({topic:['access','usability','playback','downloads','catalog','billing','support','other'],intent:['cancellation','complaint','request','praise','unclear'],severity:[1,2,3,4,5]})){el(id).add(new Option('Choose…',''));values.forEach(v=>el(id).add(new Option(v,v)));}
function render(){const r=state.reviews[index],v=state.labels[r.review_id]||{};el('progress').textContent=`${Object.keys(state.labels).length} / ${state.reviews.length} saved · Review ${index+1}`;el('identity').textContent=r.review_id;el('text').textContent=r.review_text;const {review_text,...metadata}=r;el('metadata').textContent=JSON.stringify(metadata,null,2);for(const f of ['topic','intent','severity','sentiment','evidence_quote'])el(f).value=v[f]??'';el('entities').value=(v.entities||[]).join('\n');el('needs_review').checked=v.needs_review||false;dirty=false;el('status').textContent=v.revision?'Saved human label; edits are allowed.':'';}
function move(i){if(dirty&&!confirm('Discard unsaved changes?'))return;index=i;render();}
el('prev').onclick=()=>move((index-1+state.reviews.length)%state.reviews.length);el('next').onclick=()=>move((index+1)%state.reviews.length);el('unlabeled').onclick=()=>{for(let n=1;n<=state.reviews.length;n++){let i=(index+n)%state.reviews.length;if(!state.labels[state.reviews[i].review_id])return move(i);}el('status').textContent='All reviews labeled.';};
el('form').oninput=()=>dirty=true;window.onbeforeunload=e=>{if(dirty){e.preventDefault();e.returnValue='';}};
el('form').onsubmit=async e=>{e.preventDefault();const r=state.reviews[index],payload={review_id:r.review_id,revision:state.labels[r.review_id]?.revision||0,topic:el('topic').value,intent:el('intent').value,severity:Number(el('severity').value),sentiment:Number(el('sentiment').value),entities:el('entities').value.split('\n').map(x=>x.trim()).filter(Boolean),evidence_quote:el('evidence_quote').value,needs_review:el('needs_review').checked};try{const response=await fetch('/api/labels',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const value=await response.json();if(!response.ok)throw Error(value.error);state.labels[r.review_id]=value;render();el('status').textContent='Saved successfully.';}catch(err){el('status').textContent=err.message;}};
fetch('/api/reviews').then(r=>r.json()).then(v=>{state=v;if(!v.reviews.length){el('form').hidden=true;el('status').textContent='No reviews in source.';return;}index=v.reviews.findIndex(r=>!v.labels[r.review_id]);if(index<0)index=0;render();}).catch(e=>el('status').textContent=e.message);
</script></html>'''


def serve(input_path, output_path, host='127.0.0.1', port=8765):
    if host not in ('127.0.0.1', 'localhost'):
        raise ValueError('Human labeling server must bind to localhost.')
    store = LabelStore(input_path, output_path)

    class Handler(BaseHTTPRequestHandler):
        def send(self, code, value, content_type='application/json'):
            body = value.encode() if isinstance(value, str) else json.dumps(value).encode()
            self.send_response(code)
            self.send_header('Content-Type', content_type + '; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(body)

        def permitted(self):
            host_header = self.headers.get('Host', '')
            allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            return host_header in allowed and self.headers.get('Origin', 'http://' + host_header) == 'http://' + host_header

        def do_GET(self):
            if not self.permitted():
                return self.send(403, {'error': 'Local access required.'})
            if self.path == '/':
                return self.send(200, HTML, 'text/html')
            if self.path == '/api/reviews':
                return self.send(200, store.snapshot())
            self.send(404, {'error': 'Not found.'})

        def do_POST(self):
            if not self.permitted():
                return self.send(403, {'error': 'Local access required.'})
            if self.path != '/api/labels':
                return self.send(404, {'error': 'Not found.'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 1000000 or self.headers.get('Content-Type') != 'application/json':
                    raise ValueError('Expected bounded JSON request.')
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError('Expected a JSON object.')
                self.send(200, store.save(payload))
            except (ValueError, TypeError, KeyError) as exc:
                self.send(400, {'error': str(exc)})

        def log_message(self, *_):
            pass  # Do not print golden review content or labels.

    with ThreadingHTTPServer((host, port), Handler) as server:
        print(f'Human labeling ready at http://{host}:{server.server_port}; Ctrl-C to stop.', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
