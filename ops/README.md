# Observability and metric provenance

Compose keeps its OpenTelemetry → Tempo traces and Prometheus → Grafana metrics. `ops/otel.yaml`, `ops/prometheus.yaml`, and provisioning configuration retain the local service names and discovery. The Grafana dashboard now has API request/error rates and p50/p95/p99, upload lifecycle counters, worker throughput/failures/processing latency, queue-related durable state, connection/SQL metrics, and storage throughput/errors.

Terraform's ECS module configures an ADOT sidecar for the API and each worker. It receives OTLP traces, scrapes the local API or worker metrics endpoint, exports traces to AWS X-Ray, and sends application metrics through CloudWatch EMF under `Photoplatform/Application`. Python workers send OTLP/HTTP on port 4318; the Java API uses OTLP/gRPC on 4317. Container Insights and the infrastructure dashboard provide ECS resource, ALB, RDS, and Amazon MQ metrics. Logs use CloudWatch Logs. No local Prometheus or Grafana history is migrated automatically into AWS.

| Signal | Source and interpretation |
| --- | --- |
| `http_server_requests_seconds_*` | Actual API request timings; Prometheus histogram aggregation provides p50/p95/p99. |
| `upload_sessions_total`, `upload_completed_total`, `upload_abandoned_total` | API upload lifecycle transitions. A browser S3 PUT that never calls complete is eventually abandoned by the API watchdog; this is not an S3 lifecycle-deletion receipt. |
| `media_jobs_total{outcome="completed"}` | Completed worker attempts. Cancelled jobs have a separate outcome. |
| `media_job_failures_total` | Failed processing attempts; includes transient attempts that later recover. |
| `media_processing_duration_seconds_*` | Active worker attempt duration; storage/DB wait inside the attempt is included. |
| `media_queue_wait_seconds_*` | Job creation → attempt start. Retries include earlier attempts and retry backoff; **not** broker-only queue wait. |
| `media_end_to_end_seconds_*` | Upload session creation → successful processing commit, as observed by the worker. |
| `worker_active_jobs` from a worker scrape | Handlers currently active; differs from ECS task count or RabbitMQ unacknowledged deliveries. The API exports the same metric name for durable RUNNING leases; the executing-jobs panel filters the worker scrape to avoid double counting. |
| `media_queue_depth`, `media_dead_letter_jobs` | Durable PostgreSQL job state gauges, repeated by each API replica. Grafana uses max, not sum. They are not the RabbitMQ broker queue lengths. |
| `media_oldest_queued_job_age_seconds` | Oldest currently due QUEUED/RETRY job's age since creation. It is not the broker's oldest message age. |
| `media_outbox_pending` | Durable outbox rows awaiting publication; aggregate with max across API replicas. |
| `worker_database_connections`, Hikari meters | Actual worker open connections and API pool active/idle connections. |
| `database_query_duration_seconds_*`, `worker_database_request_duration_seconds_*` | Client-observed statement/acquisition duration. They include network wait; neither is PostgreSQL server execution time or Performance Insights. |
| `database_slow_queries_total` | API statements whose client-observed duration is at least one second. No SQL text or bind values are exported. |
| `worker_storage_bytes_total` | Successful worker S3 GET/PUT bytes; repeated attempts count as transfer work. It does not include browser direct-upload traffic. |
| `worker_storage_errors_total`, `storage_request_errors_total` | Worker and API storage operation errors. Browser upload/download failures require client benchmark evidence and are not inferred from worker counters. |
| `worker_broker_connected`, `worker_broker_reconnects_total` | Per-process connection status and connection failure count; not broker health for every client. |
| `Photoplatform/Workers` metrics | [Dedicated collector](cloudwatch/README.md) samples RabbitMQ ready plus unacknowledged deliveries and ECS running task count; target tracking uses `BacklogPerTask`. |

Grafana panels intentionally show missing data when a metric has not been observed. An absent time series is not converted to zero. `up` counts reachable scrape targets, labelled as scraped processes; the CloudWatch collector is the source for actual ECS running task count. The RabbitMQ API collector refuses partial or stale snapshots instead of publishing a zero backlog. The auto scaling infrastructure must also alert on missing telemetry and avoid treating missing data as a drained queue.

CloudWatch EMF is useful for aggregated counters/gauges and alarms, but it is not a substitute for keeping Prometheus histogram buckets when calculating aggregate percentiles. The supplied Grafana percentile queries use the Prometheus data source. To retain those queries in AWS, configure a private Prometheus scraper or Amazon Managed Service for Prometheus remote-write pipeline and point Grafana to it. Do not average per-task p95 values. Benchmark artifacts carry individual timings for cloud acceptance and can calculate percentiles independently of dashboard export.

No dashboard contains made-up broker oldest-message-age, unique-image storage bytes, or server-side SQL query latency. Those need an actual source before they can be reported. Cloud metric availability, IAM authorization, ADOT export success, dashboards and alarms require verification after deployment.

References: [ADOT ECS metrics/traces configuration](https://aws-otel.github.io/docs/getting-started/ecs-configurations/ecs-config-section/), [Amazon MQ Python TLS](https://docs.aws.amazon.com/amazon-mq/latest/developer-guide/amazon-mq-rabbitmq-pika.html).
