"""Evaluate identical permission-scoped queries against keyword, semantic and hybrid retrieval."""
import argparse
import json
import math
import os
from pathlib import Path
import statistics
import time
import numpy as np
import requests


def metrics(retrieved,relevant):
    positives=set(relevant)
    if not positives: raise ValueError('Each query needs at least one labeled relevant image')
    result={f'recall@{k}':len(set(retrieved[:k]) & positives)/len(positives) for k in (5,10)}
    dcg=sum(1/math.log2(rank+2) for rank,item in enumerate(retrieved[:10]) if item in positives)
    ideal=sum(1/math.log2(rank+2) for rank in range(min(10,len(positives))))
    return {**result,'ndcg@10':dcg/ideal}


def main():
    p=argparse.ArgumentParser()
    p.add_argument('dataset',help='JSON array: query, relevant_media_ids, scope, optional teamId')
    p.add_argument('--api',default='http://localhost:8081')
    p.add_argument('--output',default='benchmarks/results/search-evaluation.json')
    args=p.parse_args()
    dataset=json.loads(Path(args.dataset).read_text())
    if not dataset: raise SystemExit('Dataset is empty')
    headers={'Authorization':'Bearer '+os.environ['TOKEN']} if os.getenv('TOKEN') else {}
    report={'queries':len(dataset),'source':str(args.dataset),'results':{},'definition':'Binary relevance; same account/scope across all modes; latency includes text encoding'}
    # Equalize text-encoder cache state before comparing request latencies.
    for example in dataset:
        params={'q':example['query'],'mode':'semantic','scope':example.get('scope','public'),'limit':10}
        if 'teamId' in example: params['teamId']=example['teamId']
        requests.get(args.api+'/api/search',params=params,headers=headers,timeout=60).raise_for_status()
    report['latencyCondition']='All query embeddings warmed first; same cache state for semantic and hybrid. Excludes cold model startup.'
    for mode in ('keyword','semantic','hybrid'):
        rows=[]
        for example in dataset:
            start=time.perf_counter()
            params={'q':example['query'],'mode':mode,'scope':example.get('scope','public'),'limit':10}
            if 'teamId' in example: params['teamId']=example['teamId']
            response=requests.get(args.api+'/api/search',params=params,headers=headers,timeout=60)
            response.raise_for_status()
            ids=[h['image']['id'] for h in response.json()['items']]
            rows.append({**metrics(ids,example['relevant_media_ids']),'latencyMs':(time.perf_counter()-start)*1000,'query':example['query'],'retrieved':ids})
        report['results'][mode]={key:statistics.mean(r[key] for r in rows) for key in ('recall@5','recall@10','ndcg@10')}
        report['results'][mode]['p95LatencyMs']=float(np.percentile([r['latencyMs'] for r in rows],95))
        report['results'][mode]['perQuery']=rows
    output=Path(args.output);output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({mode:{k:v for k,v in values.items() if k!='perQuery'} for mode,values in report['results'].items()},indent=2))


if __name__=='__main__': main()
