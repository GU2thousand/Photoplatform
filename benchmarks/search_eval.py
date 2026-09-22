"""Evaluate identical permission-scoped queries against keyword, semantic and hybrid retrieval."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import time
import numpy as np
import requests
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from benchmarks.cloud_common import revision, write_report


def metrics(retrieved,relevant):
    positives=set(relevant)
    if not positives: raise ValueError('Each query needs at least one labeled relevant image')
    result={f'recall@{k}':len(set(retrieved[:k]) & positives)/len(positives) for k in (5,10)}
    seen=set()
    dcg=0
    for rank,item in enumerate(retrieved[:10]):
        if item in positives and item not in seen: dcg+=1/math.log2(rank+2)
        seen.add(item)
    ideal=sum(1/math.log2(rank+2) for rank in range(min(10,len(positives))))
    return {**result,'ndcg@10':dcg/ideal}


def main():
    p=argparse.ArgumentParser()
    p.add_argument('dataset',help='JSON array: query, relevant_media_ids, scope, optional teamId')
    p.add_argument('--api',default='http://localhost:8081')
    p.add_argument('--output',default='benchmarks/results/search-evaluation.json')
    p.add_argument('--corpus-manifest',help='Fixed corpus provenance JSON to hash alongside query labels')
    p.add_argument('--model-version',default=os.getenv('CLIP_MODEL_VERSION','unreported'))
    args=p.parse_args()
    query_bytes=Path(args.dataset).read_bytes()
    dataset=json.loads(query_bytes)
    if not dataset: raise SystemExit('Dataset is empty')
    for example in dataset:
        if not example.get('query') or not example.get('relevant_media_ids'):
            raise SystemExit('Each query needs text and nonempty reviewed relevant_media_ids')
    headers={'Authorization':'Bearer '+os.environ['TOKEN']} if os.getenv('TOKEN') else {}
    report={'queries':len(dataset),'source':str(args.dataset),'results':{},'status':'FAIL','revision':revision(),
            'querySha256':hashlib.sha256(query_bytes).hexdigest(),'modelVersion':args.model_version,
            'corpusManifestSha256':hashlib.sha256(Path(args.corpus_manifest).read_bytes()).hexdigest() if args.corpus_manifest else None,
            'warmupFailures':[],'definition':'Binary relevance; same account/scope across all modes; latency includes text encoding; averages cover successful queries only and failures remain in denominator'}
    # Equalize text-encoder cache state before comparing request latencies.
    for example in dataset:
        params={'q':example['query'],'mode':'semantic','scope':example.get('scope','public'),'limit':10}
        if 'teamId' in example: params['teamId']=example['teamId']
        try:
            response=requests.get(args.api+'/api/search',params=params,headers=headers,timeout=60)
            if response.status_code!=200: raise RuntimeError('Warmup response failed')
        except Exception as exc:
            report['warmupFailures'].append({'query':example['query'],'errorType':type(exc).__name__})
    report['latencyCondition']='All query embeddings warmed first; same cache state for semantic and hybrid. Excludes cold model startup.'
    for mode in ('keyword','semantic','hybrid'):
        rows=[]
        for example in dataset:
            start=time.perf_counter()
            params={'q':example['query'],'mode':mode,'scope':example.get('scope','public'),'limit':10}
            if 'teamId' in example: params['teamId']=example['teamId']
            row={'query':example['query'],'scope':example.get('scope','public'),'success':False}
            try:
                response=requests.get(args.api+'/api/search',params=params,headers=headers,timeout=60)
                row['statusCode']=response.status_code
                if response.status_code!=200: raise RuntimeError('Search response failed')
                ids=[h['image']['id'] for h in response.json()['items']]
                row.update(**metrics(ids,example['relevant_media_ids']),retrieved=ids,success=True)
            except Exception as exc:
                row['errorType']=type(exc).__name__
            row['latencyMs']=(time.perf_counter()-start)*1000
            rows.append(row)
        successful=[r for r in rows if r['success']]
        report['results'][mode]={key:statistics.mean(r[key] for r in successful) if successful else None for key in ('recall@5','recall@10','ndcg@10')}
        report['results'][mode].update(attempted=len(rows),successful=len(successful),failed=len(rows)-len(successful),successRate=len(successful)/len(rows))
        report['results'][mode]['p95LatencyMs']=float(np.percentile([r['latencyMs'] for r in successful],95)) if successful else None
        report['results'][mode]['perQuery']=rows
        write_report(args.output,report)
    report['status']='PASS' if not report['warmupFailures'] and all(r['failed']==0 for r in report['results'].values()) else 'FAIL'
    write_report(args.output,report)
    print(json.dumps({mode:{k:v for k,v in values.items() if k!='perQuery'} for mode,values in report['results'].items()},indent=2))
    return 0 if report['status']=='PASS' else 1


if __name__=='__main__': sys.exit(main())
