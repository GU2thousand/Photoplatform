# Cloud acceptance and benchmark protocol

The repository provides executable measurement tools. Committing these tools does **not** establish AWS deployment, throughput, recall, cost, or recovery results. Reports from a local Compose stack remain local evidence. A tiny CI vector run establishes that the harness works against pgvector; it is not a 500K corpus benchmark or an RDS performance result.

## Environment and access

Use a disposable **dev** deployment and a short-lived AWS role (local SSO or GitHub OIDC). Every cloud write harness verifies the STS account, S3 ownership, and bucket tags `Project=photoplatform`, `Environment=dev`, `DisposableEnvironment=true` before writes. ECS scaling additionally verifies those cluster tags. It refuses a production-tagged environment. Run private MQ/DB tests from a runner inside the VPC, without opening inbound Internet access.

```bash
python -m pip install -r benchmarks/requirements-cloud.txt
export AWS_REGION=us-east-1
export EXPECTED_AWS_ACCOUNT_ID=YOUR_ACCOUNT_ID
export API_URL=https://YOUR_API_DOMAIN
export S3_BUCKET=YOUR_TERRAFORM_STORAGE_BUCKET
export CLOUDFRONT_DOMAIN=YOUR_TERRAFORM_CDN_DOMAIN
export CLOUD_FRONTEND_ORIGIN=https://YOUR_FRONTEND_DOMAIN
export STORAGE_PREFIX=''
export ALLOW_CLOUD_TEST_WRITES=1
export DISPOSABLE_ENVIRONMENT=true
```

Load `TEST_OWNER_TOKEN`, `TEST_OTHER_TOKEN`, `TEST_ADMIN_TOKEN` from the protected test environment. These must represent three distinct accounts; owner/other must have the USER role and admin the ADMIN role. Never echo them. The workload harness registers a separate disposable account for each virtual user so that the per-owner active-upload quota does not masquerade as a capacity limit. Tests delete/abort only media they create; disposable accounts persist until the environment is destroyed.

Optional `BENCHMARK_DATABASE_URL` provides **read-only** job timing evidence from the private database. It must use the deployment's TLS configuration. Without it, worker processing and queue-wait percentiles are explicitly `unmeasured`; client completion-to-ready time is never relabeled as processing time.

## S3 and CloudFront acceptance

```bash
python scripts/cloud_acceptance.py --output benchmarks/results/cloud-acceptance.json
```

This runs actual S3 PUT/GET/OPTIONS and CloudFront GET requests. It checks all four Block Public Access flags, versioning, encryption, staging expiration/noncurrent-version cleanup, browser CORS for the API's exact returned signed headers, checksum corruption, signed MIME changes, declared limits, actual length mismatch, decoded MIME mismatch, concurrent completion, unrelated-user/anonymous access, moderation, raw S3 denial, valid CloudFront delivery, actual URL expiration, and deletion.

The expiration test waits until the **original issued URLs** expire, plus clock margin; editing a signature does not count as expiration evidence. `--max-expiry-wait` defaults to 1200 seconds. An overly long TTL fails the case instead of silently skipping it. The run therefore can take approximately one upload TTL plus processing time.

New grants are denied immediately after deletion/moderation revocation. Previously issued grants can remain usable until their short signed TTL, including an already cached CloudFront object. The report records the old grant's status after deletion and requires denial after expiration. This is bounded revocation, not instantaneous invalidation of all issued URLs. Owner-level concurrent completion is tested through the API; exactly one durable job is additionally checked when the DB reader is supplied.

Each case is preserved as PASS/FAIL with sanitized evidence. Negative tests accept only their expected client-denial statuses; 5xx, network exceptions and skipped checks fail. No bearer token, signed URL, storage body, private key or database URL is serialized. Cleanup failures also fail the run. Staging lifecycle *configuration* is checked here; actual asynchronous lifecycle execution requires the separate observation in [failure-testing.md](failure-testing.md).

## Upload workloads: 20 / 50 / 100 concurrent users

```bash
python benchmarks/cloud_load.py --users 20 50 100 --iterations 5 \
  --output benchmarks/results/cloud-uploads.json
```

Each cohort uses closed-loop users: create session → S3 PUT → complete → poll READY → next iteration. Accounts are provisioned before the cohort clock starts. This measures concurrent-user latency, not an unbounded arrival-rate capacity ceiling. The default fixed JPEG is intentionally modest; its SHA-256 and bytes are recorded. Repeat with a documented, fixed input distribution before making statements about real-world upload sizes.

Reports retain **every attempted upload**, its stage, failure type, successful/failed denominator, and successful-request p50/p95/p99 for create, PUT, complete, completion-to-ready observed latency, and end-to-end latency. Failed attempts are excluded from successful latency quantiles but included in the failure rate. Each raw row identifies upload/media IDs for correlation. READY polling has up to one second of resolution. DB timings use the final attempt and expose attempt counts; they do not reconstruct per-attempt history.

## Worker workloads: 1 / 2 / 4 / 8 ECS tasks

Set `ECS_CLUSTER`, `ECS_WORKER_SERVICE`, `RABBITMQ_MANAGEMENT_URL` (HTTPS), `RABBITMQ_USERNAME`, `RABBITMQ_PASSWORD`, and optional `RABBITMQ_VHOST`/`RABBITMQ_PROCESS_QUEUE`. The default processing queue is `media.process`. Set `ALLOW_ECS_SCALING=1` only for this run.

```bash
python benchmarks/ecs_worker_scaling.py --counts 1 2 4 8 --images 100 \
  --output benchmarks/results/ecs-worker-scaling.json
```

The harness requires an initially empty queue. It saves the service's desired count and full scalable-target restoration data **before changing anything**, suspends dynamic/scheduled scaling, stops workers, prepares a fixed backlog, waits for publication, and runs each task-count cohort. It records raw uploads, terminal states, queue depth/unacknowledged messages, actual ECS running/pending tasks, queue-drain time including task startup, images/minute, and optional DB processing/queue-wait distributions. It restores the previous desired count and scaling settings in `finally`, including after failure. The original settings are retained in the JSON artifact for manual recovery after a hard process kill or runner loss; these cannot execute Python's `finally`.

CPU/memory come from real `AWS/ECS` metrics. Only full one-minute buckets inside a cohort are included; short cohorts or delayed metrics yield `unmeasured`, never zero. `--metrics-delay` allows metric publication (default 90 seconds). Increase the image cohort until it is long enough to sample resource usage. This is a **fixed task-count** benchmark; it does not prove automatic scaling convergence. Run a separate live-backlog trial with scaling enabled, preserve collector CloudWatch points, scaling activities, task count, scale-out/scale-in delays, and queue drain; require the service to return to its configured minimum. ECS metrics arrive at one-minute intervals, and scaling policies can override manual desired counts unless suspended. [AWS ECS scaling documentation](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service-auto-scaling.html).

## Exact cosine versus HNSW

The application keeps exact cosine search. This standalone experiment creates a random `vector_bench_<uuid>` schema, touches no application tables/indexes, and drops only its schema in `finally`. Use a dedicated benchmark database with pgvector installed, a resource budget and an authorized writer role. A hard kill may leave the schema behind; remove only the exact schema recorded by that run. It performs no `DROP DATABASE` and never adds an HNSW index to production tables.

```bash
export ALLOW_VECTOR_BENCHMARK_WRITES=1
# Load BENCHMARK_DATABASE_URL securely for the dedicated benchmark database.
python benchmarks/pgvector_comparison.py --sizes 10000 50000 100000 500000 \
  --dimensions 512 --queries 100 --repeats 3 \
  --output benchmarks/results/pgvector-comparison.json

# Fast correctness smoke for an isolated CI PostgreSQL service:
python benchmarks/pgvector_comparison.py --sizes 100 --dimensions 16 --queries 8 --repeats 2 \
  --output benchmarks/results/pgvector-smoke.json
```

Data are seeded unit-normal **synthetic vectors**, not image embeddings or CLIP retrieval evidence. SHA-256s for corpus/query bytes, PostgreSQL/pgvector versions, settings, revision, HNSW parameters, query plans, per-query IDs/latencies/Recall@10, p50/p95/p99, build time and index bytes are retained. Exact results supply the nearest-neighbor ground truth; this recall measures ANN approximation, not semantic relevance. HNSW runs fail if EXPLAIN does not identify the created index. Both modes warm the same queries; no cold-cache assertion is made.

Client peak RSS and SQL backend allocator snapshots are labeled separately. Neither establishes database peak resident memory or HNSW peak construction memory; the report leaves server peak memory `unmeasured`. Capture RDS Enhanced Monitoring / CloudWatch memory alongside a long real RDS experiment to fill that gap. The dimensions, distance operator and HNSW parameters follow [pgvector's primary documentation](https://github.com/pgvector/pgvector#hnsw).

## Search relevance and cost per 1,000 successful images

Use the existing labeled-query evaluator with a fixed reviewed corpus/query manifest:

```bash
python benchmarks/search_eval.py path/to/labeled-queries.json --api "$API_URL" \
  --output benchmarks/results/cloud-search.json
```

The same token, access scope and relevant-ID labels must apply to keyword/semantic/hybrid. Archive the query file hash, corpus revision/hash, encoder identity, warmup/cache conditions and raw per-query IDs. Recall@5, Recall@10 and nDCG@10 require human/task relevance labels; synthetic ANN recall cannot replace them. Search failures should remain in the new report and fail the command; a timeout is not a relevance score.

For cost, prepare an operator-reviewed JSON with `totalUSD`, `source`, `allocationMethod`, `intervalStart`, `intervalEnd`; pass it to cloud_load.py using `--cost-evidence`. Include allocated ECS API/worker/encoder, ALB, RDS, MQ, NAT, CloudFront, S3 requests/storage, logging and data transfer for the **same run interval**, explaining shared idle costs and excluded taxes/credits. The tool records evidence hash and divides by the report's successful READY-image count:

`USD / 1000 successful images = attributed total USD * 1000 / successful READY images`.

Zero successful images yields null. Without a cost file, cost remains unmeasured. A price-list estimate, Cost Explorer allocation, and actual isolated-environment billing are different evidence classes; the script labels supplied allocation and does not claim to have audited billing.

## Evidence acceptance

Commit reviewed reports only after removing account-sensitive data. Record deployment image digests/task definitions, region, machine/task sizes, dataset hashes, repetition count, warmup, test start/end, raw denominators, failed runs, and scope limits. The scripts create local JSON artifacts; they do not publish a performance claim automatically. Cloud credentials/deployment remain prerequisites. No AWS benchmark numbers are pre-filled in this repository upgrade.
