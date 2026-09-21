"""Keep broker heartbeats on the I/O thread while bounded jobs run on a worker thread."""
from concurrent.futures import ThreadPoolExecutor
import functools
import json
import logging
import os
import time
import uuid

import pika
from prometheus_client import start_http_server
from .runtime import Worker

logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
log=logging.getLogger(__name__)


def parse_message(body):
    payload=json.loads(body)
    if not isinstance(payload,dict) or not isinstance(payload.get("jobId"),str):
        raise ValueError("jobId must be a UUID string")
    return uuid.UUID(payload["jobId"]),payload


def main():
    start_http_server(int(os.getenv("METRICS_PORT","9100")))
    worker=Worker()
    queues=os.getenv("WORKER_QUEUES","media.process,media.delete").split(",")
    pool=ThreadPoolExecutor(max_workers=1)
    while True:
        try:
            params=pika.URLParameters(os.environ["RABBITMQ_URL"])
            params.heartbeat=60
            params.blocked_connection_timeout=30
            connection=pika.BlockingConnection(params)
            channel=connection.channel()
            channel.basic_qos(prefetch_count=1,global_qos=True)
            for queue in queues: channel.queue_declare(queue=queue,durable=True)

            def settle(ch, tag, success):
                if not ch.is_open: return
                if success: ch.basic_ack(tag)
                else: ch.basic_nack(tag,requeue=True)

            def consume(ch,method,properties,body):
                try:
                    job_id,payload=parse_message(body)
                except (ValueError,KeyError,TypeError):
                    log.error("Rejecting malformed message")
                    ch.basic_reject(method.delivery_tag,requeue=False); return
                carrier=dict(properties.headers or {})
                if payload.get("traceparent"): carrier["traceparent"]=payload["traceparent"]
                future=pool.submit(worker.handle,job_id,carrier)
                def completed(result,conn=connection,ack_channel=ch,tag=method.delivery_tag):
                    try: success=result.result()
                    except Exception:
                        log.exception("Job state unavailable; message will be redelivered")
                        success=False
                    if conn.is_open:
                        callback=functools.partial(settle,ack_channel,tag,success)
                        try:
                            conn.add_callback_threadsafe(callback if success else lambda: conn.call_later(5,callback))
                        except pika.exceptions.ConnectionWrongStateError: pass
                future.add_done_callback(completed)
            for queue in queues: channel.basic_consume(queue,on_message_callback=consume,auto_ack=False)
            channel.start_consuming()
        except (pika.exceptions.AMQPError,OSError):
            log.warning("Broker unavailable; reconnecting")
            time.sleep(5)


if __name__=="__main__": main()
