"""Upload a deterministic CIFAR-10 test subset for a bounded retrieval smoke benchmark.

Download cifar-10-binary.tar.gz from https://www.cs.toronto.edu/~kriz/cifar.html first.
Uses 10 images per class and five English queries per class. Class labels are proxy
relevance, not human judgments of production photographs. Never use production DBs.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import time
import uuid
import numpy as np
from PIL import Image
import requests

CLASSES = ['airplane','automobile','bird','cat','deer','dog','frog','horse','ship','truck']
QUERIES = [
 ['airplane','an airplane in the sky','a passenger jet','an aircraft','a flying plane'],
 ['automobile','a car on the road','a passenger vehicle','a motorcar','a small car'],
 ['bird','a bird with feathers','a flying bird','a small wild bird','an animal with wings'],
 ['cat','a pet cat','a kitten','a feline animal','a domestic cat'],
 ['deer','a wild deer','a deer in nature','an animal with antlers','a woodland deer'],
 ['dog','a pet dog','a puppy','a canine animal','a domestic dog'],
 ['frog','a frog','a green amphibian','a small frog','an amphibian animal'],
 ['horse','a horse','a horse in a field','an equine animal','a riding horse'],
 ['ship','a ship on water','a large boat','a vessel at sea','a sailing ship'],
 ['truck','a truck on the road','a freight vehicle','a delivery truck','a large lorry'],
]


def main():
    p=argparse.ArgumentParser()
    p.add_argument('archive')
    p.add_argument('--api',default='http://localhost:8081')
    p.add_argument('--output',default='benchmarks/results/cifar')
    args=p.parse_args()
    if os.getenv('ALLOW_BENCHMARK_WRITES')!='1': raise SystemExit('Set ALLOW_BENCHMARK_WRITES=1 for a disposable stack')
    archive=Path(args.archive)
    if hashlib.md5(archive.read_bytes()).hexdigest()!='c32a1d4ab5d03f1284b67883e8d87530':
        raise SystemExit('CIFAR-10 archive checksum mismatch')
    with tarfile.open(archive) as tar:
        data=tar.extractfile('cifar-10-batches-bin/test_batch.bin').read()
    records=np.frombuffer(data,dtype=np.uint8).reshape(-1,3073)
    account={'name':'Retrieval benchmark','email':f'eval-{uuid.uuid4().hex[:10]}@test.example','password':uuid.uuid4().hex}
    response=requests.post(args.api+'/api/auth/register',json=account,timeout=30);response.raise_for_status()
    auth=response.json();headers={'Authorization':'Bearer '+auth['token']}
    output=Path(args.output);output.mkdir(parents=True,exist_ok=True)
    # Test credentials remain local and are ignored by git.
    credentials=output/'credentials.json';credentials.write_text(json.dumps({**account,'token':auth['token']}));credentials.chmod(0o600)
    manifest=[];queries=[]
    for label,name in enumerate(CLASSES):
        ids=[]
        for index in np.flatnonzero(records[:,0]==label)[:10]:
            pixels=records[index,1:].reshape(3,32,32).transpose(1,2,0)
            buffer=io.BytesIO();Image.fromarray(pixels).save(buffer,format='PNG');payload=buffer.getvalue()
            body={'filename':f'cifar-test-{index}.png','contentType':'image/png','size':len(payload),
                  'sha256':hashlib.sha256(payload).hexdigest(),'title':f'{name} {index}',
                  'description':f'CIFAR-10 test image, labeled {name}.','category':name,'tags':name,'visibility':'PRIVATE'}
            r=requests.post(args.api+'/api/uploads',headers={**headers,'Idempotency-Key':str(uuid.uuid4())},json=body,timeout=30);r.raise_for_status();upload=r.json()
            r=requests.put(upload['uploadUrl'],headers=upload['headers'],data=payload,timeout=30);r.raise_for_status()
            r=requests.post(args.api+f"/api/uploads/{upload['uploadId']}/complete",headers=headers,timeout=30);r.raise_for_status()
            ids.append(upload['mediaId']);manifest.append({'datasetIndex':int(index),'class':name,'mediaId':upload['mediaId'],'uploadId':upload['uploadId'],'sha256':body['sha256']})
        queries.extend({'query':q,'relevant_media_ids':ids,'scope':'mine'} for q in QUERIES[label])
        print(f'Uploaded {name}',flush=True)
    (output/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    (output/'queries.json').write_text(json.dumps(queries,indent=2)+'\n')
    deadline=time.monotonic()+1200
    pending=list(manifest)
    while pending and time.monotonic()<deadline:
        remaining=[]
        for row in pending:
            r=requests.get(args.api+f"/api/uploads/{row['uploadId']}",headers=headers,timeout=30);r.raise_for_status();state=r.json()
            if state['status']=='FAILED': raise RuntimeError(state)
            if state.get('embeddingStatus')!='READY': remaining.append(row)
        pending=remaining
        print(f'Embeddings remaining: {len(pending)}',flush=True)
        if pending: time.sleep(5)
    if pending: raise RuntimeError('Timed out waiting for embeddings')


if __name__=='__main__': main()
