# Validation record — 2026-09-21

This record separates executed checks from deployment templates. Tests ran against a disposable local PostgreSQL/MinIO/RabbitMQ installation; no production data or cloud account was migrated, provisioned, or deployed.

## Executed checks

- Backend: existing Spring/H2 behavior and security suite passed (17 tests), including legacy local storage and WebSocket access behavior. New pipeline behavior is exercised separately on real PostgreSQL.
- Frontend: TypeScript/Vite build and 15 tests passed, including upload cancellation/idempotency, signed-media transport, credential isolation, and existing collaboration lifecycle tests.
- Worker: 21 unit tests passed, including real image decoding/variants/metadata and durable failure boundaries; consumer validation rejects malformed/non-string job IDs without crashing.
- Retrieval metric calculation: 3 tests passed for Recall@5/10 and nDCG@10.
- Real pipeline: 10 integration tests passed against PostgreSQL 16.14 + pgvector, MinIO, RabbitMQ and the running API/worker. Covers concurrent idempotency, conditional PUT/checksum rejection, five stored variants, private/unsigned denial, pending-public moderation/revocation, team revocation before completion, invalid-image DLQ, deletion, expiration, request validation and duplicate authorization.
- Controlled recovery: 5 integration tests passed for expired lease recovery, delete before worker claim, historical-version retry isolation, late PUT staging resweep, and hourly deleted-prefix reconciliation. These deliberately manipulate disposable database timestamps/locks; they are not a claim of broad production chaos testing.
- Real semantic permissions: 3 integration tests passed with actual CLIP embeddings and pgvector. Private and unapproved media are excluded, approval/revocation changes results, removed team members lose search access, deletion removes results, and embedding failure leaves ordinary media delivery and keyword search usable.
- Legacy migration: populated pre-Flyway schema migrated through baseline 0 and V1–V4. Three existing image IDs/paths/timestamps, membership and pending storage deletion records survived; original bytes, private/pending access denial and identity sequence were checked through the new executable API. The script creates and drops its own database.
- Real Chrome: registration, private object-store PUT, automatic PROCESSING→READY updates, decoded signed thumbnails/originals, private keyword search and logout cleanup passed with zero console errors.
- Observability: API, media-worker and embedding-worker Prometheus targets were healthy. Search/storage histogram buckets and `websocket_connections` were exported. Grafana health passed. Tempo returned a trace containing both `media-api` and `media-worker`, rooted at upload completion and continuing through queue delivery and storage/processing spans.
- Infrastructure: Terraform formatting and validation passed with Terraform 1.16.3 / AWS provider 6.65.0. Render Blueprint validated against its published schema. Merged Compose, monitoring YAML and dashboard JSON validated. These are configuration checks, not cloud deployment evidence.

Local backend verification used Java 18.0.2.1; the source and production Docker image target Java 17, also selected by CI. Native API/CLIP processes connected to Dockerized infrastructure; optional ML-container execution is a separate deployment check.

## Worker scaling: 500 images per trial

Raw data: [worker-scaling.json](benchmarks/worker-scaling.json), including all sampled container CPU/RSS values. Reproduce using [worker_scaling.py](../benchmarks/worker_scaling.py).

| Workers | Successful / attempted images | Failures | Active batch seconds | Images / minute | Processing p95 seconds | Queue wait p95 seconds |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 500 / 500 | 0 | 97.69 | 307.09 | 0.266 | 97.71 |
| 2 | 500 / 500 | 0 | 95.15 | 315.28 | 0.527 | 92.36 |
| 4 | 500 / 500 | 0 | 48.63 | 616.87 | 0.476 | 50.94 |

- Dataset: identical deterministic 1024×768 JPEG corpus, 500 distinct seeds, quality 90, 109,610,353 total input bytes. SHA-256 manifest: `861b9a1f7fac82c3936b7dcac09dc779704ea6e5f39a6864dbd0210c06134dec`.
- macOS arm64; Docker allocated 8 CPUs and 4,109,737,984 bytes RAM. Other local containers shared this allocation. Mean sampled CPU per worker was approximately 83.3%, 81.1%, and 76.4% for the three trials. Full RSS/CPU samples are retained in the raw report.
- Uploads and durable queue publication were completed before starting each worker batch. Active throughput uses first attempt start to last successful completion; processing latency excludes queue waiting. Startup-inclusive wall times were 101.52, 98.78, and 55.94 seconds. This is not browser-upload throughput.
- One trial per worker count, in 1→2→4 order, without dedicated host isolation or repeated confidence intervals. A single browser smoke upload shared the queue during the run and is excluded from the benchmark IDs. Model inference and semantic indexing were disabled during scaling.
- Four workers provided about 2.01× the single-worker throughput here; two workers gave little improvement. This demonstrates functioning independent consumers and measured local scaling, not linear scaling or production capacity.

## Retrieval: 50 queries over 100 real images

[Search results](benchmarks/search-evaluation.json), [queries/relevance labels](benchmarks/search-queries.json), [image manifest](benchmarks/search-manifest.json).

| Mode | Recall@5 | Recall@10 | nDCG@10 | Warm request p95 (ms) |
|---|---:|---:|---:|---:|
| Keyword | 0.120 | 0.240 | 0.240 | 30.61 |
| Semantic | 0.434 | 0.694 | 0.763 | 67.56 |
| Hybrid RRF | 0.446 | 0.766 | 0.817 | 80.06 |

Corpus is the first ten test images per class from the official [CIFAR-10 binary distribution](https://www.cs.toronto.edu/~kriz/cifar.html), archive MD5 `c32a1d4ab5d03f1284b67883e8d87530`. The harness verifies this digest. Images are 32×32 photographs; class labels provide ten relevant IDs per query. Titles/categories/tags include the actual class label for a legitimate keyword baseline. Five fixed English class/synonym/description queries per class were written independently of retrieval output. No dataset images or model weights are committed to the repository.

Actual OpenCLIP `ViT-B-32` / `openai` weights, QuickGELU, 512-dimensional normalized vectors and CPU inference were used. Model artifact resolved to Hugging Face snapshot `a6f597a30f7b82c51704746581f9a4e41421e878`. Both retrieval branches use the same authenticated `mine` scope and image set. Ranking uses exact pgvector cosine search and RRF constant 60. All query embeddings were warmed before the reported latency run, so hybrid does not gain an unfair cache-order advantage. Cold startup and uncached encoding are excluded from these latency numbers.

This is a functional relevance smoke benchmark with class-based proxy relevance, English-only queries, a small balanced corpus, and no train/validation tuning split. It is not a production retrieval evaluation or evidence for multilingual quality. The 50-query set includes synonyms that PostgreSQL English full-text search does not expand. Evaluate independently judged queries from the intended photograph collection before choosing production ranking or ANN indexes. Database IDs in published result files identify this disposable run; rerunning corpus preparation produces new IDs.

## Reproduction

Use an isolated stack and the environment variables described in [cloud-upgrade.md](cloud-upgrade.md). Do not target production with mutation tests or benchmarks.

```bash
# Base infrastructure and media processing.
APP_SEED_ENABLED=false docker compose up --build -d --wait
python -m venv worker/.venv
worker/.venv/bin/pip install -r worker/requirements.txt
PYTHONPATH=worker worker/.venv/bin/python -m unittest discover -s worker/tests -v
worker/.venv/bin/python -m unittest discover -s benchmarks/tests -v
ALLOW_INTEGRATION_WRITES=1 TEST_API_URL=http://localhost:8081 \
  TEST_DATABASE_URL=postgresql://generatecloud:generatecloud@localhost:5432/generatecloud \
  worker/.venv/bin/python scripts/integration_test.py
# Same environment for scripts/fault_tests.py.

# Existing-schema rehearsal uses an independent temporary DB and API port.
MIGRATION_ADMIN_DATABASE_URL=postgresql://generatecloud:generatecloud@localhost:5432/generatecloud \
  worker/.venv/bin/python scripts/migration_test.py --jar backend/build/libs/backend-0.0.1-SNAPSHOT.jar

# Optional real model services; download/cache can take several minutes.
SEMANTIC_SEARCH_ENABLED=true docker compose --profile search up --build -d
# Same integration-test environment for scripts/semantic_test.py.
ALLOW_BENCHMARK_WRITES=1 worker/.venv/bin/python benchmarks/prepare_search_corpus.py /path/to/cifar-10-binary.tar.gz
# TOKEN is the disposable corpus account's token; credentials.json is gitignored.
TOKEN='<corpus account token>' worker/.venv/bin/python benchmarks/search_eval.py benchmarks/results/cifar/queries.json
```

For scaling, set `UPLOAD_MAX_ACTIVE=1000` on the disposable API so 500 images can be prequeued; the production default remains 20. `worker_scaling.py --help` documents project/env-file parameters. [api-load.js](../benchmarks/api-load.js) measures metadata/search endpoints; [upload-load.js](../benchmarks/upload-load.js) performs concurrent real create→object PUT→complete bursts separately. Neither test measures CDN delivery over a real internet path.

## Boundaries that remain

- No AWS/Render provisioning, DNS cutover, production deployment, CDN hit-rate/egress-cost/edge-latency measurement, or cloud backup/restore drill was performed. CloudFront key handling, cache headers and private-origin configuration require verification in the target account.
- No production dataset migration, bulk historical embedding backfill, multilingual CLIP evaluation, ANN performance claim, or physical erasure of S3 noncurrent versions is implied. Existing legacy media remains readable, but must be explicitly migrated/re-uploaded before new variant/hash/embedding features apply.
- This small local run does not size production services. Media availability and embedding availability are separate; infrastructure and encoder failure must remain visible to operators.
