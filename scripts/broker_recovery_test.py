"""Verify durable outbox recovery across a real broker outage in a disposable stack.

Stops ONLY the RabbitMQ container identified by the explicit Compose project label,
restarts it in finally, and creates one test upload. Never run against production.
"""
import argparse
import json
import os
import subprocess
import time
import unittest
from urllib.parse import urlparse
import psycopg
import integration_test as integration


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--project',required=True)
    args=parser.parse_args()
    if os.getenv('ALLOW_INTEGRATION_WRITES')!='1' or urlparse(integration.API).hostname not in {'localhost','127.0.0.1'}:
        raise SystemExit('Requires explicit disposable-write opt-in and loopback TEST_API_URL')
    ids=subprocess.check_output(['docker','ps','-q','--filter',f'label=com.docker.compose.project={args.project}',
                                 '--filter','label=com.docker.compose.service=rabbitmq'],text=True).split()
    if len(ids)!=1: raise SystemExit('Expected exactly one running RabbitMQ container for this project')
    integration.PipelineIntegration.setUpClass()
    test=integration.PipelineIntegration()
    upload,_,payload=test.create()
    test.put(upload,payload)
    try:
        subprocess.run(['docker','stop',ids[0]],check=True,stdout=subprocess.DEVNULL)
        test.complete(upload)
        time.sleep(4)
        with psycopg.connect(integration.DB) as conn:
            row=conn.execute("""SELECT j.status,o.last_published_at FROM media_processing_jobs j
                JOIN media_outbox o ON o.job_id=j.id WHERE j.media_id=%s AND j.job_type='MEDIA_PROCESS'""",(upload['mediaId'],)).fetchone()
        test.assertEqual(row,('QUEUED',None),'Unconfirmed publication must remain durable and due')
    finally:
        subprocess.run(['docker','start',ids[0]],check=True,stdout=subprocess.DEVNULL)
    test.wait(upload,timeout=120)
    with psycopg.connect(integration.DB) as conn:
        test.assertEqual(conn.execute("SELECT status FROM media_processing_jobs WHERE media_id=%s AND job_type='MEDIA_PROCESS'",(upload['mediaId'],)).fetchone()[0],'DONE')
        test.assertEqual(conn.execute('SELECT count(*) FROM media_variants WHERE media_id=%s',(upload['mediaId'],)).fetchone()[0],5)
    print(json.dumps({'result':'PASS','brokerOutage':'real container stop/start','durableOutbox':'preserved','processedVariants':5}))


if __name__=='__main__': main()
