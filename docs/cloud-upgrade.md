# Cloud media deployment and operations

This guide describes the implemented pipeline and the configuration required to operate it. Local validation is recorded separately in [validation.md](validation.md). Neither the Render Blueprint nor the AWS Terraform sample has been deployed by this change.

## Local services and configuration

The default Compose stack starts PostgreSQL 16 with pgvector, MinIO, RabbitMQ, Spring Boot, one Python media worker, and the Vue frontend. The `search` profile adds a text encoder and a separate embedding worker. The media worker is deliberately independent of the large Torch/CLIP image.

```bash
docker compose up -d --build
docker compose ps
docker compose up -d --scale worker=2
# Enable search on both producers and API, not just the optional services:
SEMANTIC_SEARCH_ENABLED=true docker compose --profile search up -d --build
```

Use the same Compose project name and environment file across commands. Ports and secrets can be overridden through an untracked `.env`; if the frontend port changes, also set `FRONTEND_ORIGIN` to its actual origin. `STORAGE_ENDPOINT` is the server-reachable URL (`http://minio:9000` locally); `STORAGE_PUBLIC_ENDPOINT` is the browser-reachable signing URL (`http://localhost:9000` locally). A presigned URL contains its signed host: do not replace its hostname after generation. The same rule applies if accessing the demo from a different machine.

The application defaults to `MEDIA_PIPELINE_ENABLED=false`; Compose and the production Blueprint enable it. The frontend discovers capabilities at `/api/public/capabilities`. With the pipeline enabled, the legacy multipart upload route rejects uploads and directs clients to `/api/uploads`. Existing legacy images remain readable through their authenticated backend endpoints.

| Setting | API | Media worker | Optional ML services |
|---|---|---|---|
| `DATABASE_URL` | PostgreSQL URL; normalized into JDBC by the existing environment processor | Native PostgreSQL URL, not JDBC | Embedding worker only |
| `STORAGE_BUCKET`, `STORAGE_REGION`, `STORAGE_PREFIX` | Required, same values on all writers | Required | Embedding worker reads media |
| `STORAGE_ENDPOINT` | Internal S3 endpoint; empty for AWS SDK default | Same reachable endpoint | Embedding worker only |
| `STORAGE_PUBLIC_ENDPOINT` | Public HTTPS endpoint for signed browser URLs; empty for AWS SDK default | Not used | Not used |
| `STORAGE_ACCESS_KEY`, `STORAGE_SECRET_KEY` | Secret credentials | Secret credentials | Embedding worker only |
| `STORAGE_PATH_STYLE_ACCESS` | `false` on ordinary AWS S3, `true` in local MinIO | Must match provider | Embedding worker only |
| `RABBITMQ_HOST/PORT/USER/PASSWORD` | Broker connection | Not used | Not used |
| `SPRING_RABBITMQ_VIRTUAL_HOST`, `SPRING_RABBITMQ_SSL_ENABLED` | Managed-broker vhost and TLS | Not used | Not used |
| `RABBITMQ_URL` | Not used | `amqp://` locally; `amqps://` in managed TLS deployment | Embedding worker only |
| `SEMANTIC_SEARCH_ENABLED` | Allows semantic/hybrid requests | Enqueues embedding jobs on new processing | Set consistently with API |
| `CLIP_MODEL_VERSION` | `clip-vit-b32-openai-v1` | Passed to embedding jobs | Same version on encoder/embedding worker |
| `ENCODER_URL`, `ENCODER_TOKEN` | Internal HTTP URL and shared secret | Not used | Encoder requires matching token |
| `UPLOAD_MAX_BYTES` | Default `15728640` | Same bound on downloaded inputs | Same bound for embedding reads |
| `UPLOAD_TTL_SECONDS`, `UPLOAD_MAX_ACTIVE` | Defaults `900`, `20` | Not used | Not used |

Do not use transaction-pooling database proxies for workers: session advisory locks span external I/O and must stay attached to the same PostgreSQL connection. Use direct connections or a verified session-pooling configuration. Horizontal workers each maintain a broker connection and a database connection while handling a job; include them in connection capacity planning.

## Upload contract

Authenticate API requests with `Authorization: Bearer <token>`. Create a session with a fresh UUID `Idempotency-Key` and JSON such as:

```json
{
  "filename": "photo.jpg",
  "contentType": "image/jpeg",
  "size": 123456,
  "sha256": "<64 lowercase hex characters from the exact upload bytes>",
  "title": "Evening skyline",
  "description": "City skyline at sunset",
  "category": "Travel",
  "tags": "city,sunset",
  "visibility": "PRIVATE"
}
```

For `TEAM`, include `teamId`. A repeated key with the same request returns the existing session; reusing the key for different metadata is rejected. Supported upload formats are JPEG, PNG, WebP, GIF, and BMP. Object contents still undergo actual decoding and format checks in the worker.

1. `POST /api/uploads` returns `uploadId`, `mediaId`, `status`, `embeddingStatus`, `uploadUrl`, `headers`, `expiresAt`, `objectKey`, and optional `errorCode`.
2. PUT the exact file bytes to `uploadUrl` with the returned headers. Do not send the API bearer token to object storage. The signature binds MIME, checksum, session metadata, content length, and conditional create. Browsers set content length themselves.
3. `POST /api/uploads/{uploadId}/complete` validates the stored metadata and atomically persists processing intent. `GET /api/uploads/{uploadId}` reports progress. There is no binary upload body in the API request.
4. For a ready image, obtain a fresh authorized media URL through `/api/files/{mediaId}/url?variant=thumbnail|small|medium|large|original`.

S3 CORS must allow the exact frontend origin, methods PUT/GET/HEAD, and the returned signed headers (including `if-none-match`, checksum, content type, and `x-amz-meta-upload-id`). The AWS sample permits all request headers for this exact origin. Ensure proxies do not drop signed headers. Do not use wildcard public read policies as a CORS workaround.

## Render deployment

The [Blueprint](../render.yaml) defines the following services:

- `photoplatform-api`: Dockerized Spring Boot, `/readyz` health check, internal management port 9091, generated JWT secret, demo seeding disabled.
- `photoplatform-media-worker`: independent Python background worker consuming `media.process,media.delete`; no web ingress.
- `photoplatform-web`: static Vite build, with its API URL supplied at build time.
- `photoplatform-db`: PostgreSQL 16 with external IP access denied by default. Verify `vector` extension availability and migration privileges before upgrading an existing database.

Service plans are explicit examples, not capacity recommendations. Review the Blueprint diff and current costs before syncing: applying a Blueprint provisions or changes paid services. Check the [Render Blueprint specification](https://render.com/docs/blueprint-spec) for supported fields and validation commands.

Supply a private S3 bucket, credentials, region, and browser/server endpoints. For AWS S3, explicitly set both endpoint values to an empty string or the correct regional HTTPS endpoint; never inherit the application defaults pointing to localhost. Leave bucket autocreation disabled in production and configure exact frontend CORS separately on the API and bucket. The frontend's `VITE_API_BASE_URL` must be the API HTTPS base URL. All storage, broker, JWT, and encoder credentials belong only in server secrets.

Supply an external managed RabbitMQ broker supporting durable queues, persistent messages, publisher confirmations, and TLS. On the API set host, port (normally 5671), username, password, and the exact virtual host. `SPRING_RABBITMQ_SSL_ENABLED=true`, hostname verification, and certificate validation are enabled in the template. On the worker set the provider's `amqps://user:password@host:5671/encoded-vhost` URL. Percent-encode URL credentials/vhost; the root vhost is `%2F`. Both services must address the same broker and vhost. No broker or credentials are created by this template.

The worker copies storage settings from API environment values at Blueprint sync time. After rotating credentials, sync/redeploy affected workers as well as the API. More restrictive per-service credentials can be configured instead; the sample shares a bucket-scoped credential for simplicity. On AWS, required application operations include object Get/Put/Delete, bucket List, and HeadObject via GetObject permission; legacy storage initialization also checks bucket access. Keep access bounded to the configured bucket and prefixes.

For an existing Render database, do not attempt to change its immutable major-version property in place. Follow the provider's separate migration procedure if it is not PostgreSQL 16. Preserve the old `STORAGE_PREFIX` when upgrading an installation whose existing assets use a different prefix; synchronize that same value across the API, workers, and Terraform. The new template default must not silently change where legacy files are read.

### Optional production ML

The default Blueprint leaves semantic search disabled and avoids provisioning extra ML compute. To enable it:

1. Build an internal/private service from `worker/Dockerfile.ml`, Docker context `worker`, with command `uvicorn app.encoder:app --host 0.0.0.0 --port 8090`. Set a strong `ENCODER_TOKEN`, the shared `CLIP_MODEL_VERSION`, and `TORCH_THREADS`; allow sufficient memory for Torch and the model. Its `/health` becomes available after model initialization.
2. Build a separate background worker from the same ML Dockerfile, using the default `python -m app.consumer` command, the media worker's database/storage/broker settings, and `WORKER_QUEUES=media.embed`. It loads the image encoder locally; it does not call the text service.
3. Set `ENCODER_URL` to the encoder's private HTTP address, and set the matching `ENCODER_TOKEN` on the API. Enable `SEMANTIC_SEARCH_ENABLED=true` on the API and media workers. Match `CLIP_MODEL_VERSION` across all producers/consumers.
4. Persist or prewarm the model cache, verify health and a real text/image embedding pair, then upload a new image and wait for both `processingStatus=READY` and `embeddingStatus=READY`. Restarting with a different version does not migrate prior embeddings automatically.

Do not expose `/encode` publicly. CPU-only deployment is supported by the supplied dependencies, but resource sizing and encoder latency must be measured. Search returns 503 when disabled or the encoder is unavailable; keyword mode remains available. Existing versioned media can request an embedding replay individually; legacy media has no processed `medium` variant and needs an explicit migration/re-upload before embedding. A bulk historical backfill is not implemented.

## S3 and signed CloudFront

[infra/aws/main.tf](../infra/aws/main.tf) is a sample for a new AWS S3 bucket plus CloudFront. It creates blocked public access, bucket-owner-enforced ownership, AES-256 default encryption, versioning, exact-origin CORS, a staging lifecycle rule, an Origin Access Control, and a trusted viewer key group. The bucket policy allows this CloudFront distribution to read only the versioned media prefix and denies insecure transport. It does not create application IAM users, credentials, DNS, a custom certificate, backups, or budgets.

Set `region`, a globally unique `bucket_name`, `frontend_origin`, `storage_prefix` (matching the application), and the RSA **public** key PEM. Keep the matching private key in the API secret store, mounted as a file. Application configuration is:

```text
CDN_DOMAIN=<cdn_domain output; hostname only, no https://>
CDN_KEY_PAIR_ID=<cdn_key_pair_id output>
CDN_PRIVATE_KEY_PATH=<absolute path to private signing key>
```

Validate before any plan/apply:

```bash
terraform -chdir=infra/aws fmt -check
terraform -chdir=infra/aws init -backend=false
terraform -chdir=infra/aws validate
# Review a plan separately in your own AWS account before applying it.
```

Do not commit private keys, `.tfvars`, Terraform state, or cloud credentials. Review state-backend encryption/locking and key rotation procedures for real use.

**Every CloudFront behavior must require the trusted signing key group.** API authorization decides whether an approved public asset may receive a 60-second CDN URL. Private/team/pending media is served by short-lived S3 URLs with response cache overrides (`private, no-store`), so it never enters the public CDN delivery path. The distribution must never be a public alternative path to private keys in the same bucket. See [AWS signed URL behavior](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-signed-urls.html).

The worker writes immutable-origin cache headers on versioned objects. CloudFront caches these immutable bytes at the edge, and the sample's response headers policy limits browser `max-age` to 60 seconds. The signed-URL query fields are not part of the edge cache key; signatures are still validated on network requests. [AWS cache documentation](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/Expiration.html) explains the distinction between origin/edge TTL and browser cache headers.

Changing moderation or deleting an image prevents new authorized URLs, but does not invalidate previously issued capabilities instantly. A URL may work for the rest of its 60-second lifetime; a previously fetched public response may remain browser-fresh for 60 seconds after retrieval. Downloaded copies cannot be recalled. Immediate invalidation requirements need a separate design and actual CDN tests. CloudFront hit ratio, origin egress, latency, and revocation behavior have not been measured by the local integration tests.

## Database migration and compatibility

Flyway now owns the schema and Hibernate uses validation. `V1` creates/adopts the previous tables without renumbering IDs; the application baselines a nonempty database at version 0 so V1 and later migrations run. `V2` adds processing state and durable pipeline tables. `V3` requires `vector` and creates 512-dimensional versioned embeddings. `V4` records job timings and worker identity. Do not set Hibernate `ddl-auto=update` to bypass a failed migration.

Before upgrading an existing instance:

1. Back up the full database and object store, including legacy objects and pending `storage_deletion_jobs`. Restore a copy to isolated PostgreSQL with pgvector and verify the old image IDs and media reads there.
2. Stop writes and workers during cutover. Verify the existing schema matches the expected legacy baseline, especially identity columns, constraints, and storage prefixes. An arbitrarily drifted Hibernate schema is not guaranteed to migrate automatically.
3. Run the new API against the restored copy, inspect `flyway_schema_history`, exercise old images, direct upload, team permissions, public moderation, and deletion, then rehearse restart/retry.
4. Apply the same tested procedure to production, keep rollback backups, then start the new worker fleet and resume writes.

No migration rewrites legacy objects or invents hashes/embeddings for them. Their `storage_layout=LEGACY`, `processing_status=READY`, and `embedding_status=NOT_REQUESTED` preserve prior reads. New entries use `VERSIONED` storage and nullable legacy filename columns. The old application binary cannot safely process all new rows, so rollback after accepting versioned uploads requires restoring a compatible database/object snapshot or a forward fix. Simply setting `MEDIA_PIPELINE_ENABLED=false` is not a rollback for existing versioned assets: their delivery/deletion services require the pipeline.

## Observability and recovery

Use [compose.observability.yaml](../compose.observability.yaml) together with the base file. It adds Prometheus, an OTLP collector, Tempo, and Grafana; all ingest/query ports remain internal except loopback Grafana. The Java agent is bundled in the API image but activates only in this overlay. Python exports worker spans when an OTLP endpoint is configured. Traces may contain object identifiers and operational metadata; restrict access and choose retention appropriate to your environment.

| Signal | Source and interpretation |
|---|---|
| `upload_sessions_total`, `upload_completed_total`, `upload_abandoned_total` | API lifetime counters, labeled by Prometheus instance; reset on process restart |
| `media_queue_depth` | Durable `QUEUED` and `RETRY` job count; can include delayed retries, excludes running jobs |
| `media_dead_letter_jobs` | Database `DLQ` count; authoritative recovery list |
| `media_jobs_total`, `media_job_failures_total` | Worker outcomes and coarse failure classes |
| `media_processing_duration_seconds` | Time executing an attempt, not time waiting in the queue |
| `media_queue_wait_seconds` | Job creation to attempt start; retries include elapsed time since original creation |
| `media_end_to_end_seconds` | Upload-session creation to ready media; includes upload and queue time |
| `worker_active_jobs` | Per-worker live gauge; API also exposes a database-derived gauge; filter by scrape job to avoid double-counting |
| `storage_request_duration_seconds` | Instrumented API HEAD/delete and worker download/put/delete operations |
| `semantic_search_duration_seconds`, `semantic_search_no_result_total` | Search branch label distinguishes keyword/semantic/hybrid; latency includes text encoding |
| `websocket_connections` | Current API WebSocket connection gauge |

Prometheus uses DNS discovery for scaled media and embedding workers. API gauges query shared database state; when adding API replicas use `max`, not `sum`, for those gauges. Histograms need traffic before p95 is meaningful. Application dashboards do not provide cloud billing, CDN hit ratio, or broker internals; add provider metrics for a real deployment. Benchmark scripts capture container CPU/memory separately.

For broker outages, leave durable jobs/outbox in place and restore broker availability. Unconfirmed publications retry; queued/expired-running jobs are republished after the configured watchdog interval. Recreated broker queues may need API restart to redeclare topology. Never acknowledge/rewrite every outstanding job manually to make a dashboard look healthy.

For poison jobs, inspect `media_processing_jobs.last_error_code` and its asset/session. The database DLQ is the source of truth; messages in `media.dlq` are notifications and replaying them directly does not reset database state. Owner/admin retry is bounded by eligibility and source availability. An operator can replay a single durable DLQ job:

```bash
# DATABASE_URL must point to the intended database; this changes durable job state.
python scripts/replay_job.py <job-uuid>
```

Failed deletion needs operator replay after resolving storage permissions/outages. Processing and deletion coordinate with PostgreSQL advisory locks; allow a dead process's database connection to close before expecting recovery. The fixed lease is five minutes; extremely large/slow workloads need lease renewal or explicit longer-job design before deployment.

Readiness `/readyz` reports API/database readiness, not end-to-end RabbitMQ, object-store, or encoder health. Alert on rising queue age, publish failures, DLQ, worker scrape failures, and upload failures independently. Keep management port 9091 and worker metrics 9100 behind internal network controls; they are not public authenticated APIs.

## Backup, restore, and deletion boundaries

A recoverable backup includes PostgreSQL (all application tables, outbox/jobs, embeddings, cleanup jobs, and Flyway history), legacy and versioned media prefixes, and signing/configuration secrets stored in a separate secret system. Include staging if recovery must resume in-progress uploads. Keep the database and object-store recovery point aligned; a database snapshot alone can refer to absent files.

Restore into an isolated environment, disable writers and dispatchers until data and objects are consistent, verify a sample of originals/variants, then restart the API and workers. Jobs are at-least-once; expect duplicate deliveries after restoring a queue or replaying outbox rows. Confirm team access, public moderation, upload completion, retries, and deletions before switching traffic.

S3 versioning protects against accidental overwrites but changes deletion semantics: ordinary `DeleteObject` creates delete markers and retains noncurrent versions. The sample expires old staging versions after seven days; it does **not** permanently erase retained media versions. If permanent deletion or retention deadlines are required, add an explicit version-aware retention/erasure process and test it. Delete markers, replication, object lock, and independent backups all affect actual erasure; do not label an application `DELETED` state as proof of physical removal from every copy.

The original image is stored unchanged, including possible EXIF/GPS and animation. Only derived previews strip arbitrary metadata. If originals must be sanitized, make it an explicit product choice, change the content/hash semantics, and migrate assets deliberately.

## Performance and search evaluation

Use disposable stacks and distinct ports/project names; the worker benchmark creates accounts/media and scales workers. It requires `ALLOW_BENCHMARK_WRITES=1`, a loopback API, a matching `TEST_DATABASE_URL`, and an increased `UPLOAD_MAX_ACTIVE` appropriate for a prequeued 500-image batch. Install the harness dependencies in an isolated environment; preserve the dataset hash, worker counts, image dimensions, CPU/memory limits, version/commit, failures, and raw JSON results with your report.

The scaling harness measures active batch throughput separately from startup wall time; queue and completion timings start from durable job creation. This differs from the Prometheus end-to-end metric, which starts from session creation. Run repeated trials and representative real media before drawing capacity conclusions. k6 API load tests do not by themselves prove 20/50/100 simultaneous binary-upload behavior.

For retrieval, label 50–100 representative queries and accessible relevant IDs independently of model output. Use the same account and scope for keyword, semantic, and hybrid modes. [search_eval.py](../benchmarks/search_eval.py) accepts a JSON array such as:

```json
[
  {"query": "city skyline at sunset", "scope": "public", "relevant_media_ids": [12, 35]},
  {"query": "yellow car", "scope": "team", "teamId": 3, "relevant_media_ids": [42]}
]
```

Example IDs must be replaced with real labeled assets in the test corpus. Keep permission regression checks separate from relevance measurements. Small generated fixtures and stub encoders can verify wiring/ranking contracts but cannot establish real CLIP retrieval quality. Exact pgvector search is intentionally the baseline; introduce ANN only after measuring filtered recall, latency, and authorization behavior.

## 简体中文运维摘要

- 默认 Compose 启用直传、RabbitMQ 与独立媒体 worker；搜索还需 `search` profile，并同时将 API 和媒体 worker 的 `SEMANTIC_SEARCH_ENABLED` 设为 `true`。不同服务的存储前缀、模型版本、broker/vhost 必须一致。
- 浏览器签名地址与服务器对象存储地址可以不同，但不能在签名后替换 hostname。bucket 的 CORS 需要允许真实前端 origin、PUT/GET/HEAD 与全部签名 headers；不能通过公开 bucket 来解决跨域问题。
- Render 配置是待部署模板，应用后可能创建付费资源；需要外部托管 RabbitMQ（TLS）、私有 S3、数据库扩展权限和精确 CORS。ML 为独立私网 encoder 与 embedding worker，不默认创建。
- 旧图 ID 和路径保留，迁移不会自动转存、算 hash 或回填向量。先对数据库与对象存储做备份恢复演练，在隔离副本上验证 Flyway，再切换生产。接受新格式上传后不能直接回滚旧程序，也不能通过关闭 pipeline 完成回滚。
- worker 使用会话 advisory lock，需要直连 PostgreSQL 或已验证的会话连接池，不能使用 transaction pooling。处理、重试、DLQ 和删除状态以数据库为准，broker 消息允许重复投递。
- API 主端口的 `/readyz` 只表示 API/数据库就绪；还应监控队列积压、发布失败、DLQ、worker 与存储。9091/9100 metrics 留在内部网络。
- CloudFront 所有行为都必须验签。私密、团队、待审核图片不走公共 CDN 路径；已签发 60 秒 URL 和已下载副本无法立即撤回，公开响应还有最多 60 秒浏览器缓存。
- S3 版本管理中的普通删除产生 delete marker，未必物理删除旧版本；Terraform 仅对 staging 配置过期策略。备份、副本和正式媒体历史版本的保留/彻底清除必须另行设计。
- 原图保持原字节，可能保留 EXIF/GPS；只有派生预览去除这些元数据。不要将“预览去元数据”描述为“所有图片都已清除隐私信息”。
- 实际验证范围以 [validation.md](validation.md) 为准。扩容工具、RRF、pgvector 和模型代码存在，不代表已证明线性扩容、生产高并发或检索质量提升。
