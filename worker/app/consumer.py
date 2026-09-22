"""Keep broker heartbeats on the I/O thread while bounded jobs run separately."""
from concurrent.futures import CancelledError, ThreadPoolExecutor
import functools
import json
import logging
import os
import random
import signal
import threading
import time
import uuid

import pika
from prometheus_client import Counter, Gauge, start_http_server
from .config import rabbit_parameters
from .runtime import Worker
from .health import mark_connected, mark_disconnected

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)
RECONNECTS = Counter("worker_broker_reconnects_total", "Broker connection failures")
CONNECTED = Gauge("worker_broker_connected", "Whether this worker has a live broker connection")


def parse_message(body):
    payload = json.loads(body)
    if not isinstance(payload, dict) or not isinstance(payload.get("jobId"), str):
        raise ValueError("jobId must be a UUID string")
    return uuid.UUID(payload["jobId"]), payload


def settle(channel, delivery_tag, success):
    if channel.is_open:
        if success:
            channel.basic_ack(delivery_tag)
        else:
            channel.basic_nack(delivery_tag, requeue=True)


def reconnect_delay(attempt):
    return min(30, 2 ** min(attempt, 5)) + random.uniform(0, 1)


class Consumer:
    def __init__(self, worker):
        self.worker = worker
        self.stop = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.futures = set()
        self.futures_lock = threading.Lock()
        self.connection = None

    def has_work(self):
        with self.futures_lock:
            return bool(self.futures)

    def cancel_pending(self):
        with self.futures_lock:
            pending = list(self.futures)
        for future in pending:
            future.cancel()  # Running jobs are unaffected.

    def request_stop(self, *_):
        # Signal handlers never call Pika: broker operations stay on the I/O thread.
        self.stop.set()

    def consume(self, channel, method, properties, body):
        if self.stop.is_set():
            channel.basic_nack(method.delivery_tag, requeue=True)
            return
        try:
            job_id, payload = parse_message(body)
        except (ValueError, KeyError, TypeError):
            log.error("Rejecting malformed message")
            channel.basic_reject(method.delivery_tag, requeue=False)
            return
        carrier = dict(properties.headers or {})
        if payload.get("traceparent"):
            carrier["traceparent"] = payload["traceparent"]
        future = self.pool.submit(self.worker.handle, job_id, carrier)
        with self.futures_lock:
            self.futures.add(future)
        # Capture both: ACKs never cross a reconnect boundary.
        conn = self.connection
        def completed(result):
            try:
                success = result.result()
            except CancelledError:
                success = False
            except Exception:
                # Exception messages may contain database credentials/connection strings.
                log.warning("Job state unavailable; message will be redelivered")
                success = False
            try:
                if conn.is_open:
                    callback = functools.partial(settle, channel, method.delivery_tag, success)
                    conn.add_callback_threadsafe(callback if success else lambda: conn.call_later(5, callback))
            except pika.exceptions.ConnectionWrongStateError:
                pass
            finally:
                with self.futures_lock:
                    self.futures.discard(result)
        future.add_done_callback(completed)

    def drain(self):
        """Stop receiving, keep heartbeats alive, and give the in-flight job time."""
        deadline = time.monotonic() + int(os.getenv("WORKER_SHUTDOWN_GRACE_SECONDS", "100"))
        self.cancel_pending()
        while self.has_work() and time.monotonic() < deadline:
            if self.connection and self.connection.is_open:
                try:
                    self.connection.process_data_events(time_limit=.5)
                except (pika.exceptions.AMQPError, OSError):
                    self.connection = None
            else:
                time.sleep(.1)
        if self.connection and self.connection.is_open:
            self.connection.process_data_events(time_limit=.1)
        return not self.has_work()

    def run(self):
        attempt = 0
        mark_disconnected()
        try:
            while not self.stop.is_set():
                # A connection can drop while its job is running. Do not accumulate
                # queued work across reconnects or exceed one active job.
                while self.has_work() and not self.stop.wait(.2):
                    pass
                if self.stop.is_set():
                    break
                try:
                    self.connection = pika.BlockingConnection(rabbit_parameters())
                    channel = self.connection.channel()
                    consumer_cancelled = threading.Event()
                    channel.add_on_cancel_callback(lambda _method: consumer_cancelled.set())
                    # RabbitMQ 4.3 / quorum queues reject global QoS. Each configured
                    # queue may deliver one message; max_workers=1 processes one at
                    # a time and the futures set tracks the bounded waiting messages.
                    channel.basic_qos(prefetch_count=1, global_qos=False)
                    queues = [q.strip() for q in os.getenv("WORKER_QUEUES", "media.process,media.delete").split(",") if q.strip()]
                    if not queues:
                        raise ValueError("WORKER_QUEUES must not be empty")
                    tags = []
                    for queue in queues:
                        channel.queue_declare(queue=queue, durable=True)
                        tags.append(channel.basic_consume(queue, on_message_callback=self.consume, auto_ack=False))
                    CONNECTED.set(1)
                    connected_at = time.monotonic()
                    while not self.stop.is_set():
                        self.connection.process_data_events(time_limit=1)
                        if not channel.is_open or not self.connection.is_open or consumer_cancelled.is_set():
                            raise pika.exceptions.AMQPConnectionError("Consumer subscription lost")
                        # Main-thread heartbeat stays fresh during long image work.
                        # The ECS probe also checks a real bounded DB connection.
                        mark_connected()
                        if time.monotonic() - connected_at > 60:
                            attempt = 0
                    # basic_cancel requeues pending deliveries. The running job
                    # retains its tag until completion or connection close.
                    for tag in tags:
                        channel.basic_cancel(tag)
                except (pika.exceptions.AMQPError, OSError):
                    RECONNECTS.inc()
                    log.warning("Broker unavailable; reconnecting with capped backoff")
                    CONNECTED.set(0)
                    mark_disconnected()
                    if self.connection and self.connection.is_open:
                        try:
                            self.connection.close()
                        except pika.exceptions.AMQPError:
                            pass
                    self.connection = None
                    self.stop.wait(reconnect_delay(attempt))
                    attempt += 1
        finally:
            mark_disconnected()
            try:
                drained = self.drain()
            except (pika.exceptions.AMQPError, OSError):
                drained = not self.has_work()
            if self.connection and self.connection.is_open:
                try:
                    self.connection.close()
                except pika.exceptions.AMQPError:
                    pass
            CONNECTED.set(0)
            self.pool.shutdown(wait=drained, cancel_futures=True)
            if not drained:
                # ThreadPoolExecutor threads otherwise keep Python alive beyond ECS's
                # stop timeout. Durable lease + outbox recover this unacknowledged job.
                log.warning("Shutdown grace expired; durable job will recover after lease expiry")
                os._exit(0)


def main():
    start_http_server(int(os.getenv("METRICS_PORT", "9100")))
    consumer = Consumer(Worker())
    signal.signal(signal.SIGTERM, consumer.request_stop)
    signal.signal(signal.SIGINT, consumer.request_stop)
    consumer.run()


if __name__ == "__main__":
    main()
