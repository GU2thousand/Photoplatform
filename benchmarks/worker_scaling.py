"""Reproducible, local-only 1/2/4-worker benchmark; never invent missing results."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import time
import uuid
from urllib.parse import urlparse

import numpy as np
import psycopg
import requests
from PIL import Image,ImageDraw


def image_fixture(index):
    rng=np.random.default_rng(index)
    noise=rng.integers(0,256,(96,128,3),dtype=np.uint8)
    image=Image.fromarray(noise).resize((1024,768),Image.Resampling.BILINEAR)
    draw=ImageDraw.Draw(image)
    draw.rectangle((20,20,420,100),fill="white")
    draw.text((40,40),f"Deterministic pipeline fixture {index}",fill="black")
    stream=io.BytesIO(); image.save(stream,"JPEG",quality=90)
    return stream.getvalue()


def percentile(values,p):
    if not values: return None
    return float(np.percentile(values,p))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--api',default='http://localhost:8081')
    parser.add_argument('--project',required=True)
    parser.add_argument('--env-file')
    parser.add_argument('--count',type=int,default=500)
    parser.add_argument('--workers',type=int,nargs='+',default=[1,2,4])
    parser.add_argument('--output',default='benchmarks/results/worker-scaling.json')
    args=parser.parse_args()
    if os.getenv('ALLOW_BENCHMARK_WRITES')!='1' or urlparse(args.api).hostname not in {'localhost','127.0.0.1'}:
        raise SystemExit('Use ALLOW_BENCHMARK_WRITES=1 against a disposable localhost stack')
    root=Path(__file__).resolve().parents[1]
    command=['docker','compose','-p',args.project]
    if args.env_file: command+=['--env-file',args.env_file]
    def compose(*extra):
        subprocess.run(command+list(extra),cwd=root,check=True,stdout=subprocess.DEVNULL)
    def api(method,path,token=None,**kwargs):
        headers=kwargs.pop('headers',{})
        if token: headers['Authorization']='Bearer '+token
        response=requests.request(method,args.api+path,headers=headers,timeout=60,**kwargs)
        response.raise_for_status(); return response.json() if response.content else None
    fixtures=[image_fixture(i) for i in range(args.count)]
    manifest=[hashlib.sha256(data).hexdigest() for data in fixtures]
    report={'createdAt':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'platform':platform.platform(),
            'docker':subprocess.check_output(['docker','info','--format','{{.NCPU}} CPUs, {{.MemTotal}} bytes memory'],text=True).strip(),
            'dataset':'deterministic synthetic JPEGs; 1024x768; seed=index; quality=90',
            'count':args.count,'inputBytes':sum(map(len,fixtures)),
            'manifestSha256':hashlib.sha256(''.join(manifest).encode()).hexdigest(),
            'latencyDefinition':'successful attempt finished_at - started_at; end-to-end includes queue wait, excludes upload',
            'trials':[]}
    output=Path(args.output); output.parent.mkdir(parents=True,exist_ok=True)
    for workers in args.workers:
        compose('up','-d','--no-deps','--scale','worker=0','worker')
        run=uuid.uuid4().hex
        token=api('POST','/api/auth/register',json={'name':'Benchmark','email':f'benchmark-{run}@test.example','password':'benchmark-local-password'})['token']
        def upload(index):
            data=fixtures[index]
            session=api('POST','/api/uploads',token,headers={'Idempotency-Key':str(uuid.uuid4())},json={
                'filename':f'fixture-{index}.jpg','contentType':'image/jpeg','size':len(data),'sha256':manifest[index],
                'title':f'Benchmark {run} {index}','description':'Synthetic scaling fixture','category':'Benchmark','tags':'benchmark','visibility':'PRIVATE'})
            result=requests.put(session['uploadUrl'],headers=session['headers'],data=data,timeout=60); result.raise_for_status()
            api('POST',f"/api/uploads/{session['uploadId']}/complete",token)
            return session['mediaId']
        print(f'Preparing {args.count} uploads for {workers} workers…',flush=True)
        with ThreadPoolExecutor(max_workers=8) as pool: ids=list(pool.map(upload,range(args.count)))
        with psycopg.connect(os.environ['TEST_DATABASE_URL'],autocommit=True) as db:
            until=time.monotonic()+120
            while time.monotonic()<until:
                published=db.execute('SELECT count(*) FROM media_outbox o JOIN media_processing_jobs j ON j.id=o.job_id WHERE j.media_id=ANY(%s) AND o.last_published_at IS NOT NULL',(ids,)).fetchone()[0]
                if published==args.count: break
                time.sleep(.5)
            else: raise RuntimeError('Outbox did not publish the prepared batch')
            start=time.monotonic()
            compose('up','-d','--no-deps','--scale',f'worker={workers}','worker')
            samples=[]
            while True:
                rows=db.execute('SELECT status,count(*) FROM media_processing_jobs WHERE media_id=ANY(%s) AND job_type=\'MEDIA_PROCESS\' GROUP BY status',(ids,)).fetchall()
                states=dict(rows)
                elapsed=time.monotonic()-start
                containers=subprocess.check_output(command+['ps','-q','worker'],cwd=root,text=True).split()
                stats=[]
                if containers:
                    raw=subprocess.check_output(['docker','stats','--no-stream','--format','{{json .}}',*containers],text=True)
                    stats=[json.loads(line) for line in raw.splitlines() if line.strip()]
                samples.append({'elapsedSeconds':elapsed,'states':states,'containers':stats})
                if states.get('DONE',0)+states.get('DLQ',0)==args.count: break
                if elapsed>1800: raise RuntimeError('Worker benchmark timed out')
                time.sleep(1)
            timing=db.execute("""SELECT extract(epoch FROM finished_at-started_at),extract(epoch FROM finished_at-created_at),
              extract(epoch FROM started_at-created_at),attempt FROM media_processing_jobs
              WHERE media_id=ANY(%s) AND job_type='MEDIA_PROCESS' AND status='DONE'""",(ids,)).fetchall()
            durations=[float(row[0]) for row in timing]
            finish=db.execute("SELECT extract(epoch FROM max(finished_at)-min(started_at)) FROM media_processing_jobs WHERE media_id=ANY(%s) AND job_type='MEDIA_PROCESS'",(ids,)).fetchone()[0]
            trial={'workers':workers,'completed':len(durations),'failures':states.get('DLQ',0),'wallSecondsIncludingStartup':elapsed,
                   'activeBatchSeconds':float(finish),'imagesPerMinute':len(durations)/float(finish)*60,
                   'averageProcessingSeconds':statistics.mean(durations) if durations else None,
                   'p95ProcessingSeconds':percentile(durations,95),'p95QueueWaitSeconds':percentile([float(r[2]) for r in timing],95),
                   'p95CompletionSeconds':percentile([float(r[1]) for r in timing],95),'attempts':sum(row[3] for row in timing),'samples':samples}
            report['trials'].append(trial)
            output.write_text(json.dumps(report,indent=2)+'\n')
            print(json.dumps({k:v for k,v in trial.items() if k!='samples'}),flush=True)
    print(f'Report: {output}',flush=True)


if __name__=='__main__': main()
