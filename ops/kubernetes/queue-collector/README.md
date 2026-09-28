# Kubernetes queue metrics collector

This collector GETs RabbitMQ management queue statistics and lists Pods through the
Kubernetes API in its own namespace. It exports Prometheus metrics on port **9092**.
It uses no ECS API and no CloudWatch task count. Keep one replica to avoid duplicate
queue snapshots; deployment queue gauges must not be summed across collector replicas.

For each configured deployment, `QueueDepth = messages_ready + messages_unacknowledged`
over that target's normal work queues. The denominator is the number of matching Pods
whose phase is `Running`, whose `Ready` condition is `True`, and which have no deletion
timestamp. It is **not** RabbitMQ consumer count. Worker readiness probes must check
broker heartbeat and database availability; even then Pod readiness is not proof that
every configured consumer has registered with RabbitMQ. Validate consumer registration
and observed throughput separately before enabling automatic scaling. Do not put DLQs
in `TARGETS_JSON`, and keep a positive worker minimum replica count.

With zero Ready Pods, the real queue counts and `worker_ready_pods=0` are exported,
while the division metric is absent. No fabricated denominator of one is used.
All configured targets must succeed in one bounded collection. On any API, identity,
authentication, missing statistics, stale sample, or malformed response failure,
all queue and Pod metric series disappear and `collector_available=0`. The last
successful timestamp is retained as historical evidence. Before the first success
the timestamp is absent. Scrapes also check age, so old data cannot remain available
when the polling thread stalls. Alerts must treat unavailable/absent scaling data as
an error; fixed replicas remain a safe initial deployment choice.

RabbitMQ must expose sampled `messages_ready_details.samples` and
`messages_unacknowledged_details.samples`. Their timestamps are validated independently
against the collection clock and again after the Kubernetes call. Configure RabbitMQ's
statistics sampling; a broker that omits those samples is unavailable, not an empty
queue. The client rejects redirects, bounds response bytes, verifies TLS, and retries
only transport errors, HTTP 408/429 and 5xx within the collection time budget. Logs
contain static reason codes rather than exception strings or credentials.
The Linux main thread uses a Unix real-time alarm to enforce the total collection
deadline even against slow-drip HTTP headers/bodies; Requests inactivity timeouts
alone are insufficient. The metrics server runs separately with daemon handlers;
accepted sockets have both a five-second inactivity timeout and a five-second
absolute connection deadline, so dripping headers do not block probes or shutdown.

## Metrics and endpoints

All queue and worker metrics have `cluster`, `namespace`, and `deployment` labels:

| Metric | Meaning |
| --- | --- |
| `photoplatform_queue_ready` | Sum of ready messages in target queues |
| `photoplatform_queue_inflight` | Sum of unacknowledged messages in target queues |
| `photoplatform_queue_depth` | Ready plus inflight |
| `photoplatform_worker_ready_pods` | Matching Running, Ready, nonterminating Pods |
| `photoplatform_backlog_per_ready_pod` | Queue depth / Ready Pods; absent when no Pods are Ready |

`photoplatform_queue_collector_available` and
`photoplatform_queue_collector_last_success_timestamp_seconds` have only `cluster`
and `namespace` labels. `/metrics` returns Prometheus text, `/healthz` checks process
liveness, and `/readyz` returns 200 only while a complete fresh collection is available.
Dependencies do not determine liveness; temporary MQ/Kubernetes failures do not restart
the process. Restrict this port to cluster monitoring with NetworkPolicy; do not route
it through the public ingress.

## Runtime configuration

| Variable | Default / requirement |
| --- | --- |
| `CLUSTER_NAME` | Required; metric identity |
| `NAMESPACE` | Required; must match the namespace-scoped Role |
| `TARGETS_JSON` | `[{"deployment":"release-media-worker","queues":["media.process","media.delete"]}]` |
| `RABBITMQ_HOST` | Required hostname without URL or port |
| `RABBITMQ_MANAGEMENT_SCHEME` | `https`; HTTP allowed only with `RUNTIME_MODE=local` |
| `RABBITMQ_MANAGEMENT_PORT` | `443` |
| `RABBITMQ_VHOST` | `/` |
| `RABBITMQ_USER_FILE` | `/mnt/secrets/RABBITMQ_USER` |
| `RABBITMQ_PASSWORD_FILE` | `/mnt/secrets/RABBITMQ_PASSWORD` |
| `RABBITMQ_CA_BUNDLE` | System trust store; optional CA file path |
| `KUBERNETES_API_URL` | `https://${KUBERNETES_SERVICE_HOST}:${KUBERNETES_SERVICE_PORT_HTTPS}` or `https://kubernetes.default.svc:443` |
| `KUBERNETES_TOKEN_FILE` | `/var/run/secrets/kubernetes.io/serviceaccount/token`; reread for each list to support token rotation |
| `KUBERNETES_CA_BUNDLE` | `/var/run/secrets/kubernetes.io/serviceaccount/ca.crt` |
| `RUNTIME_MODE` | `production`; `local` allows explicitly configured HTTP and environment credentials |
| `METRICS_PORT` | `9092` |
| `METRIC_INTERVAL_SECONDS` | `30` |
| `METRIC_REQUEST_TIMEOUT_SECONDS` | `5`; connect/read timeouts and collection deadline checks |
| `METRIC_RETRY_ATTEMPTS` | `3`; allowed range 1–5 |
| `METRIC_MAX_SAMPLE_AGE_SECONDS` | `90`; future sample tolerance is 10 seconds |
| `METRIC_MAX_COLLECTION_AGE_SECONDS` | `45` |
| `METRIC_MAX_SUCCESS_AGE_SECONDS` | `90`; must exceed the collection interval |

Each target may contain a `selector` string with comma-separated equality labels.
The Helm chart supplies its release/component Pod labels, for example:

```json
[
  {"deployment":"release-media-worker","selector":"app.kubernetes.io/name=photoplatform,app.kubernetes.io/instance=release,app.kubernetes.io/component=media-worker","queues":["media.process","media.delete"]},
  {"deployment":"release-embedding-worker","selector":"app.kubernetes.io/name=photoplatform,app.kubernetes.io/instance=release,app.kubernetes.io/component=embedding-worker","queues":["embedding.compute"]}
]
```

Without `selector`, the target requires the Pod label
`photoplatform.io/deployment=<deployment>`. Selectors must be unique equality
requirements, never an unfiltered Pod list. The response namespace and labels are
checked again. Pod lists are limited to 1000 items and a truncated response fails
closed instead of creating an incorrect denominator.

Production credentials come from mounted files only, so the chart mounts Secrets
Store CSI data at `/mnt/secrets` and supplies a RabbitMQ management user with the
`monitoring` tag and read permissions on the necessary queues, no configure/write
permissions. The namespaced Kubernetes Role needs only:

```yaml
rules:
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["list"]
```

Use a dedicated service account and bound projected token. No Secrets read,
Deployment mutation, cluster-wide Pod access, or AWS permission is needed by the
collector. For isolated local tests only, `RUNTIME_MODE=local` permits missing
credential files to fall back to `RABBITMQ_USER` / `RABBITMQ_PASSWORD`, and a missing
service-account token file to fall back to `KUBERNETES_TOKEN`. TLS verification
cannot be disabled in any mode; provide trusted CA bundles for local HTTPS.

## Build and isolated verification

```sh
docker build -t photoplatform-queue-collector:local ops/kubernetes/queue-collector
python -m pip install -r ops/kubernetes/queue-collector/requirements.txt
python -m unittest discover -s ops/kubernetes/queue-collector/tests -v
```

Unit tests inject HTTP responses and clocks; they establish failure/freshness and
calculation behavior, not successful EKS deployment or Amazon MQ connectivity.
Online acceptance must retain raw metrics, actual worker Pod/consumer identities,
queue load, scaling events, dependency failure behavior, and the deployed image digest.
