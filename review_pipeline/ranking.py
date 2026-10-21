"""Recalculate the required baseline from saved records and issue membership."""
import csv
import gzip
import json
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path


def rank(records_path, membership_path, output_path, allow_multi_issue=False):
    records={}
    opener=gzip.open if str(records_path).endswith('.gz') else open
    with opener(records_path,'rt',encoding='utf-8') as f:
        for line in f:
            if not line.strip(): continue
            r=json.loads(line); rid=r['review_id']
            if rid in records: raise ValueError('Duplicate review ID: '+rid)
            records[rid]=r
    expected={rid for rid,r in records.items() if r.get('status')=='completed' and r.get('intent') in ('complaint','cancellation')}
    issues=defaultdict(list); seen=set(); assigned=set()
    with open(membership_path,newline='',encoding='utf-8') as f:
        for row in csv.DictReader(f):
            iid,rid=row['issue_id'],row['review_id']
            if not iid or rid not in expected or (iid,rid) in seen:
                raise ValueError('Invalid or duplicate issue membership')
            if not allow_multi_issue and rid in assigned: raise ValueError('Multiple issues not enabled')
            severity=records[rid].get('severity')
            if type(severity) is not int or not 1<=severity<=5: raise ValueError('Invalid severity')
            seen.add((iid,rid)); assigned.add(rid); issues[iid].append(severity)
    if assigned!=expected: raise ValueError('Complaint/cancellation records lack issue membership')
    result=[]
    for iid,values in issues.items():
        count,total=len(values),sum(values)
        mean=str((Decimal(total)/Decimal(count)).quantize(Decimal('0.000001'),rounding=ROUND_HALF_UP))
        result.append(dict(issue_id=iid,complaint_count=count,severity_sum=total,mean_severity=mean,priority_score=total))
    result.sort(key=lambda r:(-r['priority_score'],r['issue_id']))
    for i,row in enumerate(result,1): row['rank']=i
    path=Path(output_path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',newline='',encoding='utf-8') as f:
        writer=csv.DictWriter(f,fieldnames=['rank','issue_id','complaint_count','severity_sum','mean_severity','priority_score'])
        writer.writeheader();writer.writerows(result)
    return result
