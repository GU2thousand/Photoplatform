"""Publish real RabbitMQ backlog per running ECS worker; fail closed on bad data."""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import signal
import ssl
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener


LOG = logging.getLogger("photoplatform.backlog")
NAMESPACE = "Photoplatform/Workers"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
HEALTH_FILE = Path("/tmp/photoplatform-collector-health.json")


def clear_health(path=HEALTH_FILE):
    path.unlink(missing_ok=True)


def mark_healthy(published_at, path=HEALTH_FILE):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"last_success": published_at}))
    temporary.replace(path)


def is_healthy(path=HEALTH_FILE, *, now=None, max_age=180):
    try:
        timestamp = json.loads(path.read_text())["last_success"]
        if type(timestamp) not in (int, float) or not math.isfinite(timestamp):
            return False
        age = (time.time() if now is None else now) - timestamp
        return math.isfinite(max_age) and max_age > 0 and 0 <= age <= max_age
    except (OSError, ValueError, KeyError, TypeError):
        return False


class CollectionError(Exception):
    """A safe reason code only: never wrap credential-bearing exception messages."""


@dataclass(frozen=True)
class Settings:
    host: str
    username: str
    password: str
    cluster: str
    service: str
    queues: tuple[str, ...] = ("media.process", "media.delete")
    vhost: str = "/"
    port: int = 443
    scheme: str = "https"
    interval: float = 60
    timeout: float = 5
    attempts: int = 3
    max_sample_age: float = 90
    max_collection_age: float = 45
    ca_bundle: str | None = None

    def __post_init__(self):
        if not all((self.host, self.username, self.password, self.cluster, self.service)):
            raise ValueError("required collector configuration is missing")
        parsed = urlsplit("//" + self.host)
        if (parsed.hostname != self.host or parsed.path or parsed.query or parsed.fragment
                or parsed.username or parsed.port is not None):
            raise ValueError("RABBITMQ_HOST must be a hostname without URL or port")
        if self.scheme not in ("http", "https") or not 1 <= self.port <= 65535:
            raise ValueError("invalid RabbitMQ management transport")
        if not self.queues or any(not q for q in self.queues) or len(set(self.queues)) != len(self.queues):
            raise ValueError("WORKER_QUEUES must contain unique nonempty queue names")
        if not 1 <= self.attempts <= 5:
            raise ValueError("METRIC_RETRY_ATTEMPTS must be between 1 and 5")
        for value in (self.interval, self.timeout, self.max_sample_age, self.max_collection_age):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("collector durations must be finite and positive")

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        return cls(
            host=env.get("RABBITMQ_HOST", ""),
            username=env.get("RABBITMQ_USER") or env.get("RABBITMQ_USERNAME", ""),
            password=env.get("RABBITMQ_PASSWORD", ""),
            cluster=env.get("ECS_CLUSTER", ""), service=env.get("ECS_SERVICE", ""),
            queues=tuple(q.strip() for q in env.get("WORKER_QUEUES", "media.process,media.delete").split(",")),
            vhost=env.get("RABBITMQ_VHOST", "/"),
            port=int(env.get("RABBITMQ_MANAGEMENT_PORT", "443")),
            scheme=env.get("RABBITMQ_MANAGEMENT_SCHEME", "https"),
            interval=float(env.get("METRIC_INTERVAL_SECONDS", "60")),
            timeout=float(env.get("METRIC_REQUEST_TIMEOUT_SECONDS", "5")),
            attempts=int(env.get("METRIC_RETRY_ATTEMPTS", "3")),
            max_sample_age=float(env.get("METRIC_MAX_SAMPLE_AGE_SECONDS", "90")),
            max_collection_age=float(env.get("METRIC_MAX_COLLECTION_AGE_SECONDS", "45")),
            ca_bundle=env.get("RABBITMQ_CA_BUNDLE") or None,
        )


class NoRedirects(HTTPRedirectHandler):
    # A management redirect must not forward Basic credentials to another host.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def nonnegative_int(value, reason):
    if type(value) is not int or value < 0:
        raise CollectionError(reason)
    return value


class Collector:
    def __init__(self, settings, ecs, cloudwatch, *, opener=None, stopped=None,
                 monotonic=time.monotonic, now=time.time):
        self.settings = settings
        self.ecs, self.cloudwatch = ecs, cloudwatch
        self.stopped = stopped if stopped is not None else threading.Event()
        self.monotonic, self.now = monotonic, now
        self.opener = opener if opener is not None else build_opener(
            NoRedirects(), HTTPSHandler(context=ssl.create_default_context(cafile=settings.ca_bundle)))

    def _queue(self, name):
        cfg = self.settings
        path = "/api/queues/" + quote(cfg.vhost, safe="") + "/" + quote(name, safe="")
        # Request bounded recent samples: timestamps let us reject stale management statistics.
        query = urlencode({"lengths_age": 60, "lengths_incr": 5})
        url = f"{cfg.scheme}://{cfg.host}:{cfg.port}{path}?{query}"
        auth = base64.b64encode(f"{cfg.username}:{cfg.password}".encode()).decode()
        request = Request(url, headers={"Authorization": "Basic " + auth,
                                       "Accept": "application/json", "Cache-Control": "no-cache"})
        for attempt in range(cfg.attempts):
            if self.stopped.is_set():
                raise CollectionError("shutdown")
            try:
                with self.opener.open(request, timeout=cfg.timeout) as response:
                    if response.status != 200:
                        raise CollectionError("rabbitmq_http_status")
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise CollectionError("rabbitmq_response_too_large")
                    payload = json.loads(raw)
                break
            except HTTPError as exc:
                # Authentication, queue absence and redirects are configuration errors, not transient.
                exc.close()
                if exc.code not in (408, 429) and not 500 <= exc.code < 600:
                    raise CollectionError("rabbitmq_http_status") from None
                if attempt == cfg.attempts - 1:
                    raise CollectionError("rabbitmq_unavailable") from None
            except (URLError, OSError, TimeoutError):
                if attempt == cfg.attempts - 1:
                    raise CollectionError("rabbitmq_unavailable") from None
            except (ValueError, TypeError):
                raise CollectionError("rabbitmq_invalid_json") from None
            if self.stopped.wait(min(2 ** attempt, 4)):
                raise CollectionError("shutdown")
        if not isinstance(payload, dict) or payload.get("name") != name or payload.get("vhost") != cfg.vhost:
            raise CollectionError("rabbitmq_queue_identity")
        if payload.get("state") != "running":
            raise CollectionError("rabbitmq_queue_not_running")
        values = []
        sampled_at = []
        for field in ("messages_ready", "messages_unacknowledged"):
            values.append(nonnegative_int(payload.get(field), "rabbitmq_missing_queue_count"))
            details = payload.get(field + "_details")
            samples = details.get("samples") if isinstance(details, dict) else None
            if not isinstance(samples, list) or not samples:
                raise CollectionError("rabbitmq_missing_samples")
            timestamps = []
            for sample in samples:
                ts = sample.get("timestamp") if isinstance(sample, dict) else None
                if type(ts) not in (int, float) or not math.isfinite(ts) or ts < 0:
                    raise CollectionError("rabbitmq_invalid_sample_timestamp")
                timestamps.append(ts / 1000)
            age = self.now() - max(timestamps)
            if age > cfg.max_sample_age or age < -10:
                raise CollectionError("rabbitmq_stale_samples")
            sampled_at.append(max(timestamps))
        return (*values, min(sampled_at))

    def collect_and_publish(self):
        cfg = self.settings
        started = self.monotonic()
        ready = inflight = 0
        sampled_at = []
        for queue in cfg.queues:
            qready, qinflight, sample_time = self._queue(queue)
            ready += qready
            inflight += qinflight
            sampled_at.append(sample_time)
        try:
            result = self.ecs.describe_services(cluster=cfg.cluster, services=[cfg.service])
        except Exception:
            raise CollectionError("ecs_unavailable") from None
        services = result.get("services", [])
        if result.get("failures") or len(services) != 1:
            raise CollectionError("ecs_service_missing")
        service = services[0]
        if cfg.service not in (service.get("serviceName"), service.get("serviceArn")) or service.get("status") != "ACTIVE":
            raise CollectionError("ecs_service_identity_or_status")
        if cfg.cluster not in (service.get("clusterArn"), str(service.get("clusterArn", "")).rsplit("/", 1)[-1]):
            raise CollectionError("ecs_cluster_identity")
        active = nonnegative_int(service.get("runningCount"), "ecs_missing_running_count")
        if self.stopped.is_set():
            raise CollectionError("shutdown")
        if self.monotonic() - started > cfg.max_collection_age:
            raise CollectionError("collection_too_old")
        if self.now() - min(sampled_at) > cfg.max_sample_age:
            raise CollectionError("rabbitmq_stale_samples")
        depth = ready + inflight
        dimensions = [{"Name": "ClusterName", "Value": cfg.cluster.rsplit("/", 1)[-1]},
                      {"Name": "ServiceName", "Value": cfg.service.rsplit("/", 1)[-1]}]
        timestamp = datetime.fromtimestamp(self.now(), timezone.utc)
        values = {"BacklogPerTask": depth / max(active, 1), "QueueDepth": depth,
                  "Ready": ready, "InFlight": inflight, "ActiveTasks": active}
        data = [{"MetricName": key, "Value": value, "Unit": "Count", "Dimensions": dimensions,
                 "Timestamp": timestamp, "StorageResolution": 60} for key, value in values.items()]
        try:
            self.cloudwatch.put_metric_data(Namespace=NAMESPACE, MetricData=data)
        except Exception:
            raise CollectionError("cloudwatch_unavailable") from None
        return values

    def run(self):
        while not self.stopped.is_set():
            start = self.monotonic()
            try:
                values = self.collect_and_publish()
                mark_healthy(self.now())
                LOG.info("metrics_published depth=%s active_tasks=%s", values["QueueDepth"], values["ActiveTasks"])
            except CollectionError as exc:
                LOG.warning("metrics_skipped reason=%s", exc)
            except Exception:
                # Never log arbitrary exception text: SDK/HTTP exceptions can carry request secrets.
                LOG.error("metrics_skipped reason=unexpected_failure")
            self.stopped.wait(max(0.1, self.settings.interval - (self.monotonic() - start)))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Prevent SDK debug logging even when the host application enables verbose logging.
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)
    try:
        clear_health()
        settings = Settings.from_env()
        import boto3
        from botocore.config import Config
        config = Config(connect_timeout=settings.timeout, read_timeout=settings.timeout,
                        retries={"mode": "standard", "total_max_attempts": settings.attempts})
        # SDK default credential chain only; ECS task role in AWS, profile/SSO for local use.
        ecs = boto3.client("ecs", config=config)
        cloudwatch = boto3.client("cloudwatch", config=config)
        collector = Collector(settings, ecs, cloudwatch)
    except Exception:
        LOG.error("collector_start_failed reason=invalid_configuration_or_aws_setup")
        return 1
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: collector.stopped.set())
    try:
        collector.run()
    finally:
        clear_health()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
