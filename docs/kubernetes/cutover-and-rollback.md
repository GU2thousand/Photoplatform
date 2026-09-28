# Production cutover and rollback

The first implementation targets a separate EKS dev environment and API hostname. An existing ECS production entrance and consumers keep their ownership until a separately authorized production release. No production cutover has been executed by this change.

Before approving a cutover, complete real EKS dev business/fault/IAM/network/scaling/rollback rows, record source SHA and digests, test old code against the expanded schema, verify RDS backup/PITR restoration, and identify the sole owners of each shared resource. Fill the following operation record with measured thresholds and named responsibility; blanks block cutover.

| Required field | Recorded value |
| --- | --- |
| Approver and change record | Not yet configured |
| Current ECS and target EKS revision/digests | Not yet measured |
| Target production account/cluster/namespace/DB/MQ/Secret/domain identities | Not yet verified |
| Maximum 5xx rate, latency, outbox/job age and unavailable Pods | Not yet measured |
| Observation window and rollback trigger duration | Not yet selected |
| On-call owner and longest allowed rollback operation | Not yet selected |
| RPO/RTO and tested DB restore evidence | Not yet measured |
| Prior frontend object/index hash and API origin | Not yet recorded |
| Single consumer-path handoff and in-flight reconciliation plan | Not yet rehearsed |

A concrete reviewed operation sets the protected production environment gate `EKS_PRODUCTION_CUTOVER_APPROVED=1`; real production fixture checks additionally require `ALLOW_PRODUCTION_SMOKE=1`. These must remain unset until explicit production approval. Fixture accounts must be dedicated and have no unrelated real users' ownership or administrator credentials exposed in logs.

Deploy the EKS production application to its independent ALB/domain and verify digest/revision, readiness, TLS/CORS, upload signatures and authorized private delivery there. Keep frontend production publication behind the reviewed cutover. During the consumer handoff pause scaling and stop the previous media/embedding consumer path, allow or recover in-flight work using durable job/lease state, then start the selected EKS consumers. Retain idempotency/fencing and reconcile every job around the boundary. Do not silently run mixed old/new worker versions against the same queue.

Change the production frontend API origin/index only after migration and target business checks succeed, then verify the actual public served index and origin. Observe measured errors, latency, queue/job ages, Ready replicas, DB connections and memory for the recorded window. Evidence must identify the publicly accessed URL, new SHA, Helm revision, Pod UIDs/imageIDs and every failed/timeout sample.

For application rollback, preserve failure evidence, choose the last compatible chart/image digest and run `helm rollback` in the guarded context/namespace. Confirm requested previous digest and Pod UIDs, then rerun business smoke. The database stays at its migrated version. If compatibility is broken, use a reviewed forward repair or tested RDS recovery; do not automatically down-migrate.

For return to ECS, pause EKS autoscaling and consumers first, reconcile in-flight jobs and confirm EKS consumers stopped. Restore the known ECS worker version/capacity, then restore its API origin and previous frontend index; invalidate CloudFront and verify exact public index/API behavior. Preserve all job and object reconciliation evidence. Shared S3/RDS/MQ/ECR state is never destroyed during compute rollback.
