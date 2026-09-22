# RabbitMQ backlog collector

Run one independent ECS collector task to publish actual ready and unacknowledged RabbitMQ work divided by the worker service's running task count. Build using `ops/cloudwatch` as the Docker context.

```sh
docker build -t photoplatform-backlog-collector ops/cloudwatch
python3 -m unittest discover -s ops/cloudwatch/tests -v
```

## Metric and scaling contract

Namespace `Photoplatform/Workers`; dimensions exactly `ClusterName`, `ServiceName` (names, even when configuration uses ARNs); standard 60-second resolution; unit `Count`.

| Metric | Value |
| --- | --- |
| `BacklogPerTask` | `(Ready + InFlight) / max(ActiveTasks, 1)` |
| `QueueDepth` | Ready plus unacknowledged messages across all configured queues |
| `Ready` | Sum of `messages_ready` |
| `InFlight` | Sum of `messages_unacknowledged` |
| `ActiveTasks` | ECS worker service `runningCount` |

Target tracking uses `BacklogPerTask`, `Average`, these exact dimensions, and an initially configurable target such as 20. Tune it using actual image latency, throughput, and queue drain measurements. Keep the initial worker minimum at one. Running tasks are an ECS count, not proof that every consumer is ready. The zero-task denominator remains one to preserve backlog visibility.

Every configured queue is read individually, avoiding list pagination and unrelated queues. Include all queues the worker consumes; exclude DLQs. Both length counts and timestamped samples must be present. Missing queues/fields/samples, stale samples, failed HTTP/ECS calls, inactive ECS services, and excessive collection time skip the entire publication. Failed CloudWatch submissions are not replaced by zero. Genuine empty queues publish zero. Do not fill missing metrics with zero; add a missing-data alarm for the collector.

## Environment

| Variable | Default / requirement |
| --- | --- |
| `RABBITMQ_HOST` | Required bare DNS host, without URL, port, or credentials |
| `RABBITMQ_USER` | Required management user; `RABBITMQ_USERNAME` is a fallback alias |
| `RABBITMQ_PASSWORD` | Required; inject with ECS secrets from Secrets Manager |
| `RABBITMQ_VHOST` | `/` |
| `RABBITMQ_MANAGEMENT_PORT` | `443` for Amazon MQ management HTTPS |
| `RABBITMQ_MANAGEMENT_SCHEME` | `https`; `http` is for isolated local development only |
| `RABBITMQ_CA_BUNDLE` | Optional CA file; default system trust store |
| `ECS_CLUSTER`, `ECS_SERVICE` | Required cluster and **worker** service names or ARNs |
| `WORKER_QUEUES` | `media.process,media.delete`; unique comma-separated names |
| `AWS_DEFAULT_REGION` | AWS region; standard SDK region configuration is also supported |
| `METRIC_INTERVAL_SECONDS` | `60` |
| `METRIC_REQUEST_TIMEOUT_SECONDS` | `5` for HTTP and AWS connect/read operations |
| `METRIC_RETRY_ATTEMPTS` | `3` total attempts, allowed range 1–5 |
| `METRIC_MAX_SAMPLE_AGE_SECONDS` | `90` |
| `METRIC_MAX_COLLECTION_AGE_SECONDS` | `45` |
| `METRIC_HEALTH_MAX_AGE_SECONDS` | `max(180, 3 * METRIC_INTERVAL_SECONDS)` |

Management statistics and queue length history must be enabled. Requests use `lengths_age=60&lengths_incr=5` and validate both ready and unacknowledged sample timestamps. A queue without a sample during startup is skipped. Samples over the age limit or more than 10 seconds in the future are rejected; synchronize broker and container clocks. Sample age is checked again after ECS lookup. HTTP 408/429/5xx and network failures use bounded retries. Other HTTP errors fail immediately. AWS uses bounded standard SDK retries. SIGTERM interrupts interval and retry waits; in-flight requests remain bounded by I/O timeouts.

## ECS health check

Use `command = ["CMD", "python", "health.py"]`, `interval = 30`, `timeout = 5`, `retries = 3`, `startPeriod = 120`. Health requires a recent successful CloudWatch publication. The collector atomically writes `/tmp/photoplatform-collector-health.json` only after a successful complete snapshot and removes it at startup/shutdown. One failed poll does not instantly erase a recent success; repeated failures become unhealthy at the freshness limit. The file must be writable, so provide a writable `/tmp` if the root filesystem is read-only. Increase freshness/start period if the configured interval warrants it; health cannot be established merely by starting the process.

## Security and deployment

AWS authentication uses only the SDK default credential chain: an ECS task role in AWS or a normal local profile/SSO session. Task permissions are `ecs:DescribeServices` scoped to the worker service ARN and `cloudwatch:PutMetricData` on `*` with `StringEquals` condition `cloudwatch:namespace = Photoplatform/Workers`. The execution role separately needs ECR pull, Logs, and read access to the specific RabbitMQ secret and its KMS key when applicable.

Use a dedicated RabbitMQ management/monitoring user with visibility into the configured vhost and queues. The collector issues only GET requests. Run in private subnets with broker management HTTPS accessible only from the collector security group. Allow outbound ECS/CloudWatch API access through NAT or appropriate VPC endpoints. HTTPS always verifies certificates; a custom CA adds trust without bypassing verification. Redirects are rejected so Basic credentials cannot be forwarded elsewhere. Logs contain only safe reason codes and aggregate counts, never URLs, request bodies, credentials, or raw HTTP/AWS exception text.

Unit tests mock HTTP and AWS; they verify arithmetic, queue omissions, stale data, failures, retry bounds, safe logging, shutdown, and health freshness. They do not prove deployed Amazon MQ statistics or an ECS scaling action. Cloud acceptance requires observed custom metrics, target tracking alarms, 1/2/4/8-task workload measurements, and collector/broker failure tests with retained task IDs, metric exports, raw counts, and failures.

References: [RabbitMQ HTTP API](https://www.rabbitmq.com/docs/http-api-reference), [ECS DescribeServices](https://docs.aws.amazon.com/boto3/latest/reference/services/ecs/client/describe_services.html), [ECS autoscaling guidance](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/capacity-autoscaling-best-practice.html).
