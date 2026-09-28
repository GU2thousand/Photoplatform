"""Fail-closed RabbitMQ backlog metrics with Kubernetes Ready Pod denominators."""
from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import math
import os
from pathlib import Path
import re
import signal
import socket
import threading
import time
from urllib.parse import quote, urlencode, urlsplit

import requests
from prometheus_client import CollectorRegistry, CONTENT_TYPE_LATEST, generate_latest
from prometheus_client.core import GaugeMetricFamily


LOG = logging.getLogger("photoplatform.kubernetes.backlog")
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
NAME = re.compile(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?\Z")
LABEL_KEY = re.compile(r"(?:[a-z0-9.-]+/)?[A-Za-z0-9_.-]+\Z")
LABEL_VALUE = re.compile(r"[A-Za-z0-9_.-]+\Z")


class CollectionError(Exception):
    """Only predefined reason codes; upstream errors can contain credentials."""


@contextmanager
def absolute_deadline(seconds):
    """Interrupt even slow-drip headers/body on the dedicated Linux main thread."""
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "setitimer"):
        raise CollectionError("collector_requires_unix_main_thread")
    previous_handler = signal.getsignal(signal.SIGALRM)
    started = time.monotonic()

    def expired(*_):
        raise CollectionError("collection_too_old")

    signal.signal(signal.SIGALRM, expired)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0]:
            signal.setitimer(signal.ITIMER_REAL, max(0.001, previous_timer[0] - (time.monotonic() - started)), previous_timer[1])


def credentials(env, key):
    path = Path(env.get(key + "_FILE", "/mnt/secrets/" + key))
    try:
        value = path.read_text().rstrip("\r\n")
    except FileNotFoundError:
        # Only explicit local mode may use environment secrets.
        value = env.get(key, "") if env.get("RUNTIME_MODE") == "local" else ""
    if not value or "\n" in value or "\r" in value:
        raise ValueError("missing or invalid mounted RabbitMQ credentials")
    return value


def selector_labels(selector):
    labels = {}
    for pair in selector.split(","):
        key, separator, value = pair.partition("=")
        if not separator or not LABEL_KEY.fullmatch(key) or not LABEL_VALUE.fullmatch(value) or key in labels:
            raise ValueError("Pod selector must contain unique equality label requirements")
        labels[key] = value
    if not labels:
        raise ValueError("Pod selector must not be empty")
    return labels


@dataclass(frozen=True)
class Target:
    deployment: str
    queues: tuple[str, ...]
    selector: str = ""

    def __post_init__(self):
        if not NAME.fullmatch(self.deployment) or len(self.deployment) > 253:
            raise ValueError("invalid deployment name")
        if not self.selector:
            object.__setattr__(self, "selector", "photoplatform.io/deployment=" + self.deployment)
        selector_labels(self.selector)
        if (not 1 <= len(self.queues) <= 20 or any(not isinstance(q, str) or not q for q in self.queues)
                or len(set(self.queues)) != len(self.queues)):
            raise ValueError("target queues must be unique nonempty names")


@dataclass(frozen=True)
class Settings:
    host: str
    username: str = field(repr=False)
    password: str = field(repr=False)
    cluster: str
    namespace: str
    targets: tuple[Target, ...]
    kubernetes_url: str = "https://kubernetes.default.svc:443"
    token_file: str = str(SERVICE_ACCOUNT / "token")
    kubernetes_ca: str | bool = str(SERVICE_ACCOUNT / "ca.crt")
    token: str = field(default="", repr=False)
    scheme: str = "https"
    port: int = 443
    vhost: str = "/"
    rabbitmq_ca: str | bool = True
    runtime_mode: str = "production"
    interval: float = 30
    timeout: float = 5
    attempts: int = 3
    max_sample_age: float = 90
    max_collection_age: float = 45
    max_success_age: float = 90
    metrics_port: int = 9092

    def __post_init__(self):
        parsed = urlsplit("//" + self.host)
        if (not self.host or parsed.hostname != self.host or parsed.path or parsed.query or parsed.fragment
                or parsed.username or parsed.port is not None):
            raise ValueError("RABBITMQ_HOST must be a hostname without URL or port")
        if not self.username or not self.password or ":" in self.username:
            raise ValueError("invalid RabbitMQ credentials")
        if not self.cluster or not NAME.fullmatch(self.namespace) or len(self.namespace) > 63:
            raise ValueError("cluster and namespace are required")
        api = urlsplit(self.kubernetes_url)
        if (api.scheme not in ("http", "https") or not api.hostname or api.username or api.password
                or api.path not in ("", "/") or api.query or api.fragment):
            raise ValueError("invalid Kubernetes API URL")
        if self.scheme not in ("http", "https") or not 1 <= self.port <= 65535:
            raise ValueError("invalid RabbitMQ management transport")
        if self.runtime_mode != "local" and (self.scheme != "https" or api.scheme != "https"):
            raise ValueError("HTTP is permitted only in RUNTIME_MODE=local")
        if self.rabbitmq_ca is False or self.kubernetes_ca is False:
            raise ValueError("TLS verification must remain enabled")
        if self.token and self.runtime_mode != "local":
            raise ValueError("Kubernetes token environment fallback is local only")
        if (not 1 <= len(self.targets) <= 20
                or len({target.deployment for target in self.targets}) != len(self.targets)):
            raise ValueError("targets must contain unique deployments")
        if not 1 <= self.attempts <= 5 or not 1 <= self.metrics_port <= 65535:
            raise ValueError("invalid retry count or metrics port")
        for duration in (self.interval, self.timeout, self.max_sample_age, self.max_collection_age, self.max_success_age):
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError("collector durations must be finite and positive")
        if self.interval >= self.max_success_age:
            raise ValueError("collection interval must be shorter than maximum success age")

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        raw_targets = json.loads(env.get("TARGETS_JSON", '[{"deployment":"release-media-worker",'
                                       '"queues":["media.process","media.delete"]}]'))
        if not isinstance(raw_targets, list):
            raise ValueError("TARGETS_JSON must be an array")
        targets = tuple(Target(item["deployment"], tuple(item["queues"]), item.get("selector", ""))
                        for item in raw_targets)
        api = env.get("KUBERNETES_API_URL") or "https://" + env.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc") \
            + ":" + env.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        return cls(host=env.get("RABBITMQ_HOST", ""), username=credentials(env, "RABBITMQ_USER"),
                   password=credentials(env, "RABBITMQ_PASSWORD"), cluster=env.get("CLUSTER_NAME", ""),
                   namespace=env.get("NAMESPACE", ""), targets=targets, kubernetes_url=api,
                   token_file=env.get("KUBERNETES_TOKEN_FILE", str(SERVICE_ACCOUNT / "token")),
                   kubernetes_ca=env.get("KUBERNETES_CA_BUNDLE") or str(SERVICE_ACCOUNT / "ca.crt"),
                   token=env.get("KUBERNETES_TOKEN", ""), scheme=env.get("RABBITMQ_MANAGEMENT_SCHEME", "https"),
                   port=int(env.get("RABBITMQ_MANAGEMENT_PORT", "443")), vhost=env.get("RABBITMQ_VHOST", "/"),
                   rabbitmq_ca=env.get("RABBITMQ_CA_BUNDLE") or True,
                   runtime_mode=env.get("RUNTIME_MODE", "production"), interval=float(env.get("METRIC_INTERVAL_SECONDS", "30")),
                   timeout=float(env.get("METRIC_REQUEST_TIMEOUT_SECONDS", "5")),
                   attempts=int(env.get("METRIC_RETRY_ATTEMPTS", "3")),
                   max_sample_age=float(env.get("METRIC_MAX_SAMPLE_AGE_SECONDS", "90")),
                   max_collection_age=float(env.get("METRIC_MAX_COLLECTION_AGE_SECONDS", "45")),
                   max_success_age=float(env.get("METRIC_MAX_SUCCESS_AGE_SECONDS", "90")),
                   metrics_port=int(env.get("METRICS_PORT", "9092")))


def nonnegative_int(value, reason):
    if type(value) is not int or value < 0:
        raise CollectionError(reason)
    return value


@dataclass(frozen=True)
class Observation:
    deployment: str
    ready: int
    inflight: int
    ready_pods: int
    sampled_at: float


class Collector:
    def __init__(self, settings, *, session=None, stopped=None, now=time.time, monotonic=time.monotonic):
        self.settings = settings
        self.session = session if session is not None else requests.Session()
        self.session.trust_env = False  # Ignore implicit proxy/netrc credentials.
        self.stopped = stopped if stopped is not None else threading.Event()
        self.now, self.monotonic = now, monotonic
        self.started = 0.0

    def _deadline(self):
        remaining = self.settings.max_collection_age - (self.monotonic() - self.started)
        if self.stopped.is_set():
            raise CollectionError("shutdown")
        if remaining <= 0:
            raise CollectionError("collection_too_old")
        return remaining

    def _json(self, url, *, source, headers=None, auth=None, verify=True):
        cfg = self.settings
        for attempt in range(cfg.attempts):
            timeout = min(cfg.timeout, self._deadline())
            response = None
            try:
                response = self.session.get(url, headers={"Accept": "application/json", "Cache-Control": "no-cache",
                                                          **(headers or {})}, auth=auth, verify=verify,
                                            timeout=(timeout, timeout), allow_redirects=False, stream=True)
                if response.status_code != 200:
                    if response.status_code in (408, 429) or 500 <= response.status_code < 600:
                        raise requests.RequestException()
                    raise CollectionError(source + "_http_status")
                raw = bytearray()
                for chunk in response.iter_content(chunk_size=16384):
                    self._deadline()
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise CollectionError(source + "_response_too_large")
                self._deadline()
                try:
                    result = json.loads(raw)
                except (ValueError, TypeError, UnicodeError):
                    raise CollectionError(source + "_invalid_json") from None
                if not isinstance(result, dict):
                    raise CollectionError(source + "_invalid_json")
                return result
            except requests.RequestException:
                if attempt == cfg.attempts - 1:
                    raise CollectionError(source + "_unavailable") from None
            finally:
                if response is not None:
                    response.close()
            if self.stopped.wait(min(2 ** attempt, 4, self._deadline())):
                raise CollectionError("shutdown")
        raise CollectionError(source + "_unavailable")

    def _queue(self, name):
        cfg = self.settings
        path = "/api/queues/" + quote(cfg.vhost, safe="") + "/" + quote(name, safe="")
        url = f"{cfg.scheme}://{cfg.host}:{cfg.port}{path}?" + urlencode({"lengths_age": 60, "lengths_incr": 5})
        payload = self._json(url, source="rabbitmq", auth=(cfg.username, cfg.password), verify=cfg.rabbitmq_ca)
        if payload.get("name") != name or payload.get("vhost") != cfg.vhost:
            raise CollectionError("rabbitmq_queue_identity")
        if payload.get("state") != "running":
            raise CollectionError("rabbitmq_queue_not_running")
        values, timestamps = [], []
        for field_name in ("messages_ready", "messages_unacknowledged"):
            values.append(nonnegative_int(payload.get(field_name), "rabbitmq_missing_queue_count"))
            details = payload.get(field_name + "_details")
            samples = details.get("samples") if isinstance(details, dict) else None
            if not isinstance(samples, list) or not samples:
                raise CollectionError("rabbitmq_missing_samples")
            field_timestamps = []
            for sample in samples:
                ts = sample.get("timestamp") if isinstance(sample, dict) else None
                if type(ts) not in (int, float) or not math.isfinite(ts) or ts < 0:
                    raise CollectionError("rabbitmq_invalid_sample_timestamp")
                field_timestamps.append(ts / 1000)
            newest = max(field_timestamps)
            age = self.now() - newest
            if not -10 <= age <= cfg.max_sample_age:
                raise CollectionError("rabbitmq_stale_samples")
            timestamps.append(newest)
        return (*values, min(timestamps))

    def _ready_pods(self, target):
        cfg = self.settings
        try:
            token = Path(cfg.token_file).read_text().strip()
        except OSError:
            token = cfg.token if cfg.runtime_mode == "local" else ""
        if not token or "\n" in token or "\r" in token:
            raise CollectionError("kubernetes_token_missing")
        query = urlencode({"labelSelector": target.selector, "limit": 1000})
        url = cfg.kubernetes_url.rstrip("/") + "/api/v1/namespaces/" + quote(cfg.namespace, safe="") + "/pods?" + query
        payload = self._json(url, source="kubernetes", headers={"Authorization": "Bearer " + token}, verify=cfg.kubernetes_ca)
        metadata, pods = payload.get("metadata"), payload.get("items")
        if payload.get("kind") != "PodList" or not isinstance(pods, list) or not isinstance(metadata, dict):
            raise CollectionError("kubernetes_invalid_pod_list")
        # Do not silently turn a partial list into a smaller scaling denominator.
        if metadata.get("continue"):
            raise CollectionError("kubernetes_pod_list_truncated")
        labels = selector_labels(target.selector)
        ready, identities = 0, set()
        for pod in pods:
            if not isinstance(pod, dict):
                raise CollectionError("kubernetes_invalid_pod")
            meta, status = pod.get("metadata"), pod.get("status")
            if not isinstance(meta, dict) or not isinstance(status, dict):
                raise CollectionError("kubernetes_invalid_pod")
            pod_labels, uid = meta.get("labels"), meta.get("uid")
            if (meta.get("namespace") != cfg.namespace or not uid or not isinstance(uid, str)
                    or uid in identities or not isinstance(pod_labels, dict)
                    or any(pod_labels.get(key) != value for key, value in labels.items())):
                raise CollectionError("kubernetes_pod_identity")
            identities.add(uid)
            conditions = status.get("conditions", [])
            if not isinstance(conditions, list) or any(not isinstance(condition, dict) for condition in conditions):
                raise CollectionError("kubernetes_invalid_pod")
            ready_conditions = [condition for condition in conditions if condition.get("type") == "Ready"]
            if len(ready_conditions) > 1:
                raise CollectionError("kubernetes_invalid_pod")
            if (not meta.get("deletionTimestamp") and status.get("phase") == "Running"
                    and ready_conditions and ready_conditions[0].get("status") == "True"):
                ready += 1
        return ready

    def collect(self):
        with absolute_deadline(self.settings.max_collection_age):
            return self._collect()

    def _collect(self):
        self.started = self.monotonic()
        observations = []
        for target in self.settings.targets:
            ready, inflight, sampled_at = 0, 0, []
            for queue in target.queues:
                qready, qinflight, timestamp = self._queue(queue)
                ready += qready
                inflight += qinflight
                sampled_at.append(timestamp)
            observations.append(Observation(target.deployment, ready, inflight, self._ready_pods(target), min(sampled_at)))
        self._deadline()
        if any(not -10 <= self.now() - item.sampled_at <= self.settings.max_sample_age for item in observations):
            raise CollectionError("rabbitmq_stale_samples")
        return tuple(observations)

    def run(self, metrics):
        while not self.stopped.is_set():
            start = self.monotonic()
            try:
                observations = self.collect()
                metrics.success(observations)
                LOG.info("collection_succeeded targets=%d", len(observations))
            except CollectionError as exc:
                metrics.failure()
                LOG.warning("collection_failed reason=%s", exc)
            except Exception:
                metrics.failure()
                LOG.error("collection_failed reason=unexpected_failure")
            self.stopped.wait(max(0.1, self.settings.interval - (self.monotonic() - start)))


class QueueMetrics:
    """Atomic snapshots: collection failures immediately remove all queue series."""

    def __init__(self, settings, *, now=time.time):
        self.settings, self.now = settings, now
        self.lock = threading.Lock()
        self.observations = None
        self.last_success = None

    def success(self, observations):
        with self.lock:
            self.observations = observations
            self.last_success = self.now()

    def failure(self):
        with self.lock:
            self.observations = None

    def snapshot(self):
        with self.lock:
            observations, timestamp = self.observations, self.last_success
        now = self.now()
        available = (observations is not None and timestamp is not None
                     and 0 <= now - timestamp <= self.settings.max_success_age
                     and all(-10 <= now - item.sampled_at <= self.settings.max_sample_age for item in observations))
        return observations if available else None, timestamp

    def describe(self):
        # Registration must not capture an initial success or create fake queue zeros.
        return []

    def collect(self):
        observations, timestamp = self.snapshot()
        cfg = self.settings
        labels = ["cluster", "namespace"]
        values = [cfg.cluster, cfg.namespace]
        available = GaugeMetricFamily("photoplatform_queue_collector_available", "1 when a complete current collection exists", labels=labels)
        available.add_metric(values, int(observations is not None))
        yield available
        if timestamp is not None:
            success = GaugeMetricFamily("photoplatform_queue_collector_last_success_timestamp_seconds", "Unix timestamp of last complete successful collection", labels=labels)
            success.add_metric(values, timestamp)
            yield success
        if observations is None:
            return
        labels.append("deployment")
        definitions = (
            ("photoplatform_queue_ready", "RabbitMQ ready messages across this deployment's normal work queues", lambda item: item.ready),
            ("photoplatform_queue_inflight", "RabbitMQ unacknowledged messages across this deployment's normal work queues", lambda item: item.inflight),
            ("photoplatform_queue_depth", "RabbitMQ ready plus unacknowledged messages", lambda item: item.ready + item.inflight),
            ("photoplatform_worker_ready_pods", "Matching Running Ready nonterminating Kubernetes Pods", lambda item: item.ready_pods),
            ("photoplatform_backlog_per_ready_pod", "Queue depth divided by Ready Pods; absent with zero Ready Pods", lambda item: (item.ready + item.inflight) / item.ready_pods if item.ready_pods else None),
        )
        for name, description, value in definitions:
            metric = GaugeMetricFamily(name, description, labels=labels)
            for item in observations:
                sample = value(item)
                if sample is not None:
                    metric.add_metric(values + [item.deployment], sample)
            if metric.samples:
                yield metric


def make_handler(registry, metrics, stopped):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/metrics":
                status, body, content_type = 200, generate_latest(registry), CONTENT_TYPE_LATEST
            elif self.path == "/healthz":
                status = 503 if stopped.is_set() else 200
                body, content_type = b"process_running\n", "text/plain"
            elif self.path == "/readyz":
                status = 200 if metrics.snapshot()[0] is not None else 503
                body, content_type = b"collection_available\n" if status == 200 else b"collection_unavailable\n", "text/plain"
            else:
                status, body, content_type = 404, b"not_found\n", "text/plain"
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass  # Paths/headers are untrusted and may carry secrets.

    return Handler


class MetricsServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_deadline = 5

    # An inactivity timeout alone cannot stop headers that drip indefinitely.
    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(self.request_deadline)
        return connection, address

    @staticmethod
    def expire_connection(connection):
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def process_request_thread(self, request, client_address):
        timer = threading.Timer(self.request_deadline, self.expire_connection, args=(request,))
        timer.daemon = True
        timer.start()
        try:
            super().process_request_thread(request, client_address)
        finally:
            timer.cancel()

    def handle_error(self, *_):
        LOG.info("metrics_request_failed reason=io_failure")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    try:
        settings = Settings.from_env()
        collector, metrics = Collector(settings), QueueMetrics(settings)
        registry = CollectorRegistry()
        registry.register(metrics)
        server = MetricsServer(("0.0.0.0", settings.metrics_port), make_handler(registry, metrics, collector.stopped))
    except Exception:
        LOG.error("collector_start_failed reason=invalid_configuration")
        return 1
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: collector.stopped.set())
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True)
    thread.start()
    try:
        # Signal alarms enforce one wall-clock budget across DNS, headers, retries,
        # and streamed bodies; they must run on the Unix process's main thread.
        collector.run(metrics)
    finally:
        collector.stopped.set()
        metrics.failure()
        server.shutdown()
        server.server_close()
        thread.join(timeout=6)
        collector.session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
