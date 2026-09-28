# Kubernetes observability

The Helm chart is the only application deployment source. Its optional telemetry
topology runs one Prometheus metrics scraper, one OTLP trace collector and one
namespace-limited kube-state-metrics instance. API management port 9091 remains
Pod-only; workers expose 9100, encoder `/metrics` uses internal 8090, and the queue
collector uses 9092. None is routed through ALB. Scraped series include cluster,
namespace, deployment, Pod, release commit and image digest. Application traces
carry the same resource identity; the OTEL collector does not scrape Prometheus
again. Configure a genuine verified HTTPS OTLP backend, not an assumed AWS X-Ray
endpoint. This chart uses generic OTLP and no AWS telemetry IAM role.

Canonical configs here are copied into `deploy/helm/photoplatform/files` for chart
portability; chart tests reject divergence. The dashboard is included as a
`grafana_dashboard=1` ConfigMap for an existing Grafana provisioner. Prometheus
keeps 24 hours in bounded ephemeral storage. It restarts empty; use reviewed remote
storage if long-term metrics retention is required. Alert rules are installed in
Prometheus but no email/Slack/Alertmanager routing is configured by this upgrade.
Candidate thresholds must be tuned from real dev measurements.

Platform bootstrap owns read-only Roles/RoleBindings for these names:

| ServiceAccount / Role | Namespace resources | Verbs |
| --- | --- | --- |
| `photoplatform-queue-collector` | core `pods` | `list` |
| `photoplatform-prometheus` | core `pods` | `get,list,watch` |
| `photoplatform-kube-state-metrics` | core `pods`, apps `deployments`, autoscaling `horizontalpodautoscalers` | `list,watch` |

The chart owns the ServiceAccounts but not RBAC. Set the relevant
`rbacProvisioned=true` only after bootstrap verification. Kubernetes tokens are
mounted only on these read-only observers; API, workers, encoder and migrator do
not receive a Kubernetes API token. IAM roles and CSI Secrets remain per workload.
The collector needs a separate MQ monitoring user and its own two-key Secret;
it sends only HTTPS GET, never mutation requests. See
[`queue-collector/README.md`](queue-collector/README.md).

Ready Pod counts are availability observations, not verified broker consumer
counts. No Ready Pods means queue depth remains visible but the ratio is absent.
Collection failure or stale samples removes queue series and publishes unavailable
status; do not coalesce absent samples to zero. DLQs are excluded. Initial worker
capacity is fixed and never scales to zero. A production external-metric adapter
and measured hysteresis remain prerequisites for automatic backlog scaling.

Durable database gauges (`media_outbox_pending`, `media_queue_depth`, job age and
DLQ) are repeated on API Pods and use `max`, not `sum`. Broker queue lengths are
separate collector series. This avoids inflating queue totals with API replicas.
Rules include API failures/latency, due job/outbox age, DLQ, broker reconnect state,
DB pool waits, stale collection, unavailable replicas, OOM, CrashLoop and Pending.
Encoder mismatch alerts use the actual encoder request counter; readiness and
`encoder_model_ready{model_version}` show warmup. They do not establish semantic
retrieval quality, which requires the fixed-corpus acceptance report.

Required verification after deploy: inspect target count and unique Pod identity,
inject bounded MQ/DB outages and confirm unavailable samples without restarts,
check state-metrics source/permissions, trigger an alert in disposable dev, and
retain raw series plus collection time. Local injected API tests and chart
rendering are recorded separately from these real-cloud observations.

References: [Kubernetes Deployment](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/),
[NetworkPolicy](https://kubernetes.io/docs/concepts/services-networking/network-policies/),
[ASCP Pod Identity and SecretProviderClass](https://github.com/aws/secrets-store-csi-driver-provider-aws/blob/main/README.md),
[kube-state-metrics probes](https://github.com/kubernetes/kube-state-metrics/blob/main/examples/standard/deployment.yaml).
