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
from .health import mark_alive, mark_stopped, mark_connected, mark_disconnected

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
        self.gc_thread = None
        self.running = False
        self.shutdown_deadline = None
        self.shutdown_complete = threading.Event()
        self.shutdown_watchdog = None

    def guard_shutdown_deadline(self):
        remaining = max(0, self.shutdown_deadline - time.monotonic())
        if not self.shutdown_complete.wait(remaining):
            # Pika cancellation/close RPCs can block across broker heartbeat
            # failure. One absolute deadline covers RPCs and active-job drain.
            log.warning("Total shutdown grace expired; unacknowledged work will recover")
            os._exit(0)

    def arm_shutdown_watchdog(self):
        if self.running and self.shutdown_watchdog is None:
            self.shutdown_watchdog = threading.Thread(target=self.guard_shutdown_deadline,
                name="shutdown-deadline", daemon=True)
            self.shutdown_watchdog.start()

    def garbage_loop(self):
        interval = max(10, int(os.getenv("WORKER_GC_INTERVAL_SECONDS", "60")))
        while not self.stop.wait(interval):
            try:
                self.worker.collect_garbage()
            except Exception:
                # The attempt registry remains due after a dependency failure.
                log.warning("Attempt object cleanup unavailable; durable sweep will retry")

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
        if self.shutdown_deadline is None:
            self.shutdown_deadline = time.monotonic() + max(0, int(os.getenv("WORKER_SHUTDOWN_GRACE_SECONDS", "100")))
        self.stop.set()
        self.arm_shutdown_watchdog()

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
        deadline = self.shutdown_deadline if self.shutdown_deadline is not None else (
            time.monotonic() + max(0, int(os.getenv("WORKER_SHUTDOWN_GRACE_SECONDS", "100"))))
        self.cancel_pending()
        while self.has_work() and time.monotonic() < deadline:
            mark_alive()
            if self.connection and self.connection.is_open:
                try:
                    self.connection.process_data_events(time_limit=.5)
                except (pika.exceptions.AMQPError, OSError):
                    self.connection = None
            else:
                time.sleep(.1)
        if self.connection and self.connection.is_open and time.monotonic() < deadline:
            self.connection.process_data_events(time_limit=.1)
        return not self.has_work()

    def run(self):
        attempt = 0
        self.running = True
        if self.stop.is_set():
            self.arm_shutdown_watchdog()
        mark_disconnected()
        mark_alive()
        self.gc_thread = threading.Thread(target=self.garbage_loop, name="attempt-gc", daemon=True)
        self.gc_thread.start()
        try:
            while not self.stop.is_set():
                mark_alive()
                # A connection can drop while its job is running. Do not accumulate
                # queued work across reconnects or exceed one active job.
                while self.has_work() and not self.stop.wait(.2):
                    mark_alive()
                if self.stop.is_set():
                    break
                try:
                    self.connection = pika.BlockingConnection(rabbit_parameters())
                    mark_alive()
                    channel = self.connection.channel()
                    mark_alive()
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
                        mark_alive()
                        tags.append(channel.basic_consume(queue, on_message_callback=self.consume, auto_ack=False))
                        mark_alive()
                    CONNECTED.set(1)
                    connected_at = time.monotonic()
                    while not self.stop.is_set():
                        mark_alive()
                        self.connection.process_data_events(time_limit=1)
                        if not channel.is_open or not self.connection.is_open or consumer_cancelled.is_set():
                            raise pika.exceptions.AMQPConnectionError("Consumer subscription lost")
                        # Main-thread heartbeat stays fresh during long image work.
                        # Readiness checks a bounded DB connection; liveness does not.
                        mark_connected()
                        if time.monotonic() - connected_at > 60:
                            attempt = 0
                    # basic_cancel requeues pending deliveries. The running job
                    # retains its tag until completion or connection close.
                    for tag in tags:
                        channel.basic_cancel(tag)
                        mark_alive()
                except (pika.exceptions.AMQPError, OSError):
                    mark_alive()
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
                    mark_alive()
                    self.stop.wait(reconnect_delay(attempt))
                    attempt += 1
        finally:
            self.request_stop()
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
            if self.gc_thread:
                # Cleanup owns no message ACK. Its durable cursor survives abrupt
                # process loss; it must not exhaust the workload's drain budget.
                self.gc_thread.join(timeout=1)
            mark_stopped()
            self.shutdown_complete.set()
            self.running = False
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
