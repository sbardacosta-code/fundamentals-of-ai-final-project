"""Offline-first commands. No command in this foundation invokes a provider."""
import argparse
import json
from pathlib import Path

from .storage import Store, atomic_json, digest


def config_for(role='enrich'):
    from . import jev
    return dict(role=role,model=jev.MODEL,prompt_version=jev.PROMPT_VERSION,
                schema_version=jev.SCHEMA_VERSION,adapter_sha256=digest(Path(jev.__file__).read_text()),
                ambiguity_threshold=0.5,input_fields=['review_text'],evidence_policy='full-original-text-v1')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    prepare=sub.add_parser('prepare',help='Ingest/profile CSV and queue unique nonempty texts; zero model calls')
    prepare.add_argument('--input',required=True); prepare.add_argument('--db',default='artifacts/reviews.sqlite')
    prepare.add_argument('--output',default='artifacts/prepared.json')
    status=sub.add_parser('status');status.add_argument('--db',default='artifacts/reviews.sqlite');status.add_argument('--config')
    export=sub.add_parser('export',help='Export current states honestly, including pending; not a completed grading submission')
    export.add_argument('--db',default='artifacts/reviews.sqlite');export.add_argument('--dataset',required=True)
    export.add_argument('--config',required=True);export.add_argument('--output',default='artifacts/export')
    label=sub.add_parser('label',help='Local human-only labeling interface; no predictions')
    label.add_argument('--input',default='data/source/golden_50_to_label.csv');label.add_argument('--output',default='data/human/golden_labels.jsonl')
    label.add_argument('--port',type=int,default=8765)
    evaluate=sub.add_parser('evaluate');evaluate.add_argument('--golden',required=True);evaluate.add_argument('--predictions',required=True);evaluate.add_argument('--output',required=True)
    ranking=sub.add_parser('rank');ranking.add_argument('--records',required=True);ranking.add_argument('--membership',required=True);ranking.add_argument('--output',required=True);ranking.add_argument('--allow-multi-issue',action='store_true')
    cost=sub.add_parser('cost-replay');cost.add_argument('--calls',required=True);cost.add_argument('--rates',required=True);cost.add_argument('--output',required=True);cost.add_argument('--volume',type=int,default=660622)
    args=parser.parse_args(argv)
    if args.command=='label':
        from .labeling import serve
        serve(args.input,args.output,port=args.port);return
    if args.command=='evaluate':
        from .evaluation import evaluate
        result=evaluate(args.golden,args.predictions,args.output)
    elif args.command=='rank':
        from .ranking import rank
        result=rank(args.records,args.membership,args.output,args.allow_multi_issue)
    elif args.command=='cost-replay':
        from .cost import replay
        result=replay(args.calls,args.rates,args.output,args.volume)
    else:
        store=Store(args.db)
        try:
            if args.command=='prepare':
                profile=store.ingest(args.input);config=config_for();cid=store.prepare(profile['file_sha256'],config)
                result=dict(dataset_id=profile['file_sha256'],config_id=cid,config=config,profile=profile,status=store.status(cid),paid_execution=False)
                atomic_json(args.output,result)
            elif args.command=='status':result=store.status(args.config)
            else:
                if not store.db.execute('SELECT 1 FROM datasets WHERE id=?',(args.dataset,)).fetchone() or not store.db.execute('SELECT 1 FROM configs WHERE id=?',(args.config,)).fetchone():
                    raise ValueError('Unknown dataset or configuration')
                folder=Path(args.output);folder.mkdir(parents=True,exist_ok=True)
                counts={}
                with (folder/'records.jsonl').open('w',encoding='utf-8') as f:
                    for row in store.records(args.dataset,args.config):
                        counts[row['status']]=counts.get(row['status'],0)+1
                        f.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n')
                with (folder/'calls.jsonl').open('w',encoding='utf-8') as f:
                    for row in store.db.execute('SELECT payload FROM calls WHERE config_id=? ORDER BY created',(args.config,)):f.write(row[0]+'\n')
                atomic_json(folder/'checkpoint.json',store.snapshot(args.dataset,args.config))
                result=dict(states=counts,complete=all(k in ('completed','quarantined') for k in counts),note='State export only; full grading adapter and live orchestration are not yet implemented.')
                atomic_json(folder/'export_status.json',result)
        finally:store.close()
    print(json.dumps(result,indent=2,ensure_ascii=False))
