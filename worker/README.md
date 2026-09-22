# Media workers in Compose and ECS

The same consumer image runs in either environment. `STORAGE_PROVIDER=minio` keeps the local endpoint and path-style addressing; `STORAGE_PROVIDER=aws` uses the SDK credential chain and virtual-hosted S3 addressing. In AWS, no explicit `STORAGE_ACCESS_KEY`, `STORAGE_SECRET_KEY`, or `STORAGE_ENDPOINT` is passed to boto3. ECS task-role credentials refresh automatically. Standard AWS credential-chain inputs still work for a developer running the image outside ECS.

| Setting | Compose | ECS |
| --- | --- | --- |
| Storage | `STORAGE_ENDPOINT`, `STORAGE_ACCESS_KEY`, `STORAGE_SECRET_KEY` | `STORAGE_PROVIDER=aws`, `STORAGE_BUCKET`, `STORAGE_REGION`, task role |
| Object prefix | `STORAGE_PREFIX=generate-cloud` | `STORAGE_PREFIX=` for `staging/` and `media/` at bucket root |
| Database | `DATABASE_URL` | `DATABASE_HOST`, `DATABASE_PORT`, `DATABASE_NAME`, `DATABASE_USER`, `DATABASE_PASSWORD` |
| Database TLS | Local default `prefer` | `DATABASE_SSL_MODE=verify-full`; CA `/app/certs/global-bundle.pem` |
| RabbitMQ | `RABBITMQ_URL=amqp://…` | `RABBITMQ_HOST`, `RABBITMQ_PORT=5671`, `RABBITMQ_USER`, `RABBITMQ_PASSWORD`, `RABBITMQ_SSL_ENABLED=true` |
| Queues | `WORKER_QUEUES=media.process,media.delete` | Same; independent embedding worker uses `media.embed` |
| Traces | `OTEL_EXPORTER_OTLP_ENDPOINT=http://otel:4318` | `http://127.0.0.1:4318` for an ADOT sidecar |

Passwords are passed as discrete fields, so characters such as `@`, `/`, `?`, and `%` do not need URI escaping. ECS injects values from Secrets Manager at task start; rotating a secret requires replacing the running task. No application code retrieves Secrets Manager secrets itself. The worker fails configuration checks if AWS database TLS is weaker than `verify-full` or AMQP is plaintext. AMQPS verifies both the certificate chain and hostname. A custom broker trust bundle can be set through `RABBITMQ_CA_FILE`.

Database connection acquisition makes at most three attempts by default, with 1-second and 2-second delays and a 5-second per-attempt connect timeout. `DATABASE_CONNECT_ATTEMPTS` is bounded to 1–5. SQL statements have a 30-second timeout, configurable through `DATABASE_STATEMENT_TIMEOUT_MS`. A SQL transaction is never automatically replayed after an ambiguous failure. Durable claims, advisory locks, leases, and the API outbox watchdog own recovery. S3 calls have three total SDK attempts, a 5-second connect timeout, and a 30-second read timeout.

RabbitMQ heartbeats stay on the main I/O thread while a single executor thread processes images. Prefetch is one **per consumer**, compatible with quorum queues and RabbitMQ 4.3; global QoS is not used. With the default two queues, a task may hold two unacknowledged deliveries but executes one job at a time. All waiting Futures are tracked across disconnects. Reconnects use exponential delay capped at 30 seconds plus at most one second of jitter; retries continue while the process is alive, allowing a broker to recover after an extended outage.

On SIGTERM, the worker stops accepting work, cancels queued work that has not started, and gives its active job `WORKER_SHUTDOWN_GRACE_SECONDS` (default 100) to finish while continuing broker heartbeats. ECS has a 120-second stop timeout. If the grace expires, the process exits without acknowledging incomplete work. The existing five-minute database lease and outbox watchdog recover it. The code preserves at-least-once delivery: it does not claim exactly-once external I/O. Duplicate object writes are deterministic; versioned S3 may retain earlier object versions until lifecycle cleanup. Erasure deletes every version and delete marker under the media prefix.

## Targeted failure injection

Only in a disposable test task, set both `DISPOSABLE_ENVIRONMENT=true` and `WORKER_FAULT_AFTER_S3_JOB_ID=<exact job UUID>`. After all variant writes and before the metadata transaction, this task exits with code 86. Run replacement workers **without** the override, or the same job will crash repeatedly. This is an explicit process-loss test; it does not weaken ordinary job behavior. The cloud failure runbook is in `docs/failure-testing.md`.

## Metrics and verification

The metrics endpoint stays on port 9100. The original processing, job-age, end-to-end and storage timing metrics remain. Additional counters measure successful storage payload bytes, failed storage operations, client database query/acquire latency and failures, open worker DB connections, broker connectivity, and reconnect failures. Successful transfers include retry traffic; they are not unique-image counts. Cancelled jobs are counted separately from completed jobs so they do not inflate throughput.

Run unit tests from the repository root:

```sh
PYTHONPATH=worker python -m unittest discover -s worker/tests -v
python -m unittest discover -s ops/cloudwatch/tests -v
```

These tests cover configuration, TLS requirements, bounded acquisition retry, cancellation, multiple queue deliveries, ACK connection ownership, the S3/DB fault boundary, and existing image and durable-job behavior without connecting to cloud services. An actual ECS shutdown/recovery run, RDS certificate validation, Amazon MQ reconnect, and S3 transfer remain deployment acceptance tests; passing these unit tests does not establish cloud throughput or recovery time.

Primary references: [Amazon MQ Python/Pika TLS](https://docs.aws.amazon.com/amazon-mq/latest/developer-guide/amazon-mq-rabbitmq-pika.html), [RabbitMQ 4.3 changes](https://docs.aws.amazon.com/amazon-mq/latest/developer-guide/rabbitmq-43.html), [RDS TLS](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/PostgreSQL.Concepts.General.SSL.html).

ECS readiness runs `python -m app.health` (10-second timeout, 30-second interval). A JSON timestamp in `/tmp` is refreshed only by the subscribed, connected broker I/O loop, even while an image job is running. A missing or >15-second-old heartbeat fails readiness. Each check additionally opens one short-lived database connection with a 3-second connect timeout and a 1-second `SELECT 1` statement timeout. Authentication or TLS failures in either dependency therefore prevent an ECS rolling deployment from considering the worker healthy. Broker disconnect, subscription cancellation, and shutdown remove readiness. The probe tests database connectivity, not every schema permission or S3 operation; cloud smoke tests still verify those contracts.
