# Failure injection and recovery evidence

These are executable tools and operator runbooks for a disposable AWS **dev** deployment. They are not a record of completed cloud fault tests. Keep the failed run, recovery timestamps, task/image revisions, job/media IDs, before/after database state and CloudWatch logs for every trial. Local container-stop tests establish local behavior only.

Use the guarded environment in [cloud-benchmarks.md](cloud-benchmarks.md). AWS writes require short-lived credentials, the expected STS account, and disposable project/environment tags. Use a dedicated test environment with no other users. Private RDS/MQ tests run within the VPC. Never open RDS/MQ to `0.0.0.0/0`, purge shared queues, disable a VPC, or stop an entire production broker to run these tests. Fault scenarios below are independent; restore one completely before starting another.

## 1. One ECS worker stops during processing

Create one test upload through the normal API, upload its S3 object, complete it, and select a worker service task while its durable MEDIA_PROCESS job is RUNNING. Set `BENCHMARK_DATABASE_URL` to a private read-capable database connection and set `ECS_CLUSTER`/`ECS_WORKER_SERVICE`. Listing candidates is read-only:

```bash
aws ecs list-tasks --cluster "$ECS_CLUSTER" --service-name "$ECS_WORKER_SERVICE" --desired-status RUNNING
export ALLOW_ECS_FAILURE_INJECTION=1
python scripts/cloud_failure.py --task-arn "$SELECTED_WORKER_TASK_ARN" --upload-id "$TEST_UPLOAD_ID" \
  --timeout 900 --output benchmarks/results/cloud-worker-failure.json
```

The tool verifies the task belongs to the exact disposable worker service, refuses an upload that is no longer processing, records the durable RUNNING job, stops **one selected task**, waits for READY, requires exactly one DONE processing job, and checks service capacity recovers. It changes no desired count or scaling policy. ECS maintains the service; the existing lease/outbox watchdog provides redelivery. Allow more than the worker's five-minute lease before declaring a recovery failure.

ECS StopTask sends a graceful termination signal; this worker drains its active job before exiting when possible. A successful run proves service replacement and job completion, not necessarily abrupt job interruption. An arbitrary stopped worker may also not own the selected job. The tool explicitly does **not** certify that association: correlate the job's `worker_id`, task metadata and job logs, or use the deterministic hook below, before claiming that a particular in-flight job was interrupted. Preserve the recovery report's failure even if the service later recovers. Also inspect five unique variants and their SHA-256s; one DONE job alone is insufficient to prove correct output content.

## 2. Worker dies after S3 writes and before database commit

The worker implements a narrowly targeted fault hook:

```text
DISPOSABLE_ENVIRONMENT=true
WORKER_FAULT_AFTER_S3_JOB_ID=<one MEDIA_PROCESS UUID>
```

It exits the process with code 86 **after all variant PUTs and before the transaction that inserts variants/marks READY**. It is disabled without the explicit disposable flag and does not affect other job IDs. Do not put this hook into the normal service definition: repeatedly replacing a permanently fault-configured task would crash the same job indefinitely.

Procedure:

1. Save the dev worker's desired count and Application Auto Scaling target settings. Suspend all three scaling modes and temporarily set worker count to zero, as the scaling benchmark does; wait for zero running tasks. Do not proceed during another workload.
2. Prepare one upload and complete it. Read its MEDIA_PROCESS UUID from the private DB; retain its upload/media IDs. Leave the API/outbox service available.
3. Launch **one standalone** Fargate task using the same worker task definition, execution/task roles, subnets and worker security group. Supply only the two fault variables in its container override. `aws ecs run-task` accepts `--overrides file://work/fault-overrides.json` and `--network-configuration file://work/worker-network.json`; construct those files from this deployment's inspected worker container name/network configuration. Store them under `work/` with owner-only permissions. Do not modify the ECS service task definition.
4. Wait for exit code 86. Before restarting normal consumption, record the job's RUNNING state/lease and query `media_variants` for that media/version. The durable variant rows must still be absent; S3 `list-object-versions` for that **single media prefix** must show the variant writes. If this evidence is missing, the fault location was not proven.
5. Restore the service's original desired count and autoscaling target. Its normal task has no fault override. After lease expiration/redelivery, require one DONE job, READY media, five unique variant rows and correct content hashes. Record the attempt increment and recovery interval.
6. Delete the test image through the API and verify its media prefix, including S3 noncurrent versions, is erased. Keep version-list evidence for any leftovers; overwrite retries can create old versions in a versioned bucket.

The restore operation is required even if the injected task never runs. Record exact original counts/suspension flags before step 1 and restore them from a second terminal if the operator session fails. The cloud scaling report's `restorePlan` is an example of the required evidence shape.

## 3. Amazon MQ unavailable: durable outbox accumulation and drain

The safe target is a disposable test deployment, with one explicit MQ security-group ingress rule from the application's security group on TCP 5671. Avoid shutting down/rebooting a shared Amazon MQ broker. Record the exact rule and its group/source tags and IDs before the experiment:

```bash
aws ec2 describe-security-group-rules --security-group-rule-ids "$TEST_MQ_RULE_ID" \
  > work/mq-rule-before.json
```

Inspect that file and prepare `work/mq-restore-permissions.json` as the equivalent `IpPermissions` array (`IpProtocol`, `FromPort`, `ToPort`, and `UserIdGroupPairs` for the same source SG). The restoring command is:

```bash
aws ec2 authorize-security-group-ingress --group-id "$TEST_MQ_SECURITY_GROUP" \
  --ip-permissions file://work/mq-restore-permissions.json
```

In an interactive shell, install this command as an EXIT/INT/TERM cleanup trap **before** calling `revoke-security-group-ingress --group-id "$TEST_MQ_SECURITY_GROUP" --security-group-rule-ids "$TEST_MQ_RULE_ID"`. Limit the fault interval to 60 seconds. Security groups are stateful: removing a rule may leave existing connections usable, so first prove the publisher's connection has failed using logs/metrics. If necessary, start a fresh dev API task connection during the interval; a still-connected publisher is a failed injection, not a successful outage test. Do not claim the broker itself was down when only this flow was blocked.

Complete a prepared upload during the proven connection outage. Read (do not mutate) the database:

```sql
SELECT j.id,j.media_id,j.status,j.attempt,o.last_published_at
FROM media_processing_jobs j JOIN media_outbox o ON o.job_id=j.id
WHERE j.media_id = :test_media_id AND j.job_type='MEDIA_PROCESS';
```

Require the API transaction to commit once, a queued durable job, an unconfirmed/due outbox row (`last_published_at IS NULL`), and increasing publisher failure evidence. Restore the exact ingress flow, confirm connectivity, and require confirmed publication, eventual READY, one processing job and five distinct variants. Save the outbox backlog curve and actual drain interval. A successful API request without the DB/outbox evidence is insufficient.

## 4. Duplicate delivery, poison message and replay

In the disposable environment, obtain one known job UUID and its routing key. Publish that same UUID twice using the existing RabbitMQ exchange/message envelope, after inspecting the worker consumer contract; never invent a second database job for the same media/version. Require one final DONE job and one row per variant. Preserve original/redelivered broker message IDs and worker logs. Duplicate messages that encounter an active lock may be acknowledged; the durable lease/outbox watchdog owns later recovery.

For poison input, create a legitimate upload session whose bytes are not a decodable image, but whose declared SHA-256/length and S3 MIME metadata match those bytes. S3 upload/completion should work; decoding should result in durable DLQ/FAILED with `INVALID_IMAGE`. Permanent invalid images are rejected after the first attempt; transient errors use the bounded retry budget. Distinguish the durable database DLQ state from the RabbitMQ dead-letter queue and record both independently. A malformed broker envelope is a separate consumer test and should not be counted as an image-processing retry trial.

Inspect `scripts/replay_job.py` before replaying one UUID from the durable DLQ; it checks asset lifecycle and source retention. `POST /api/images/{id}/retry?type=MEDIA_PROCESS` is the owner/admin API path. Replaying permanently invalid bytes will fail again, which is correct. For a successful replay demonstration, use a transient failure whose cause has been repaired, preserve the original failure, and then verify one READY result. Never purge the DLQ to make dashboards look healthy.

## 5. Temporary RDS connection failure

Use the same single-flow procedure as MQ, targeting **only** the disposable RDS security group's TCP 5432 ingress rule from the app SG. Save `work/rds-rule-before.json`, construct its exact restore permissions, install the restore trap, impose a short bounded interval, and restore it. Existing pooled connections may survive a security-group change; force a new test connection and prove timeout/failure rather than assuming the rule edit interrupted traffic.

During the proven outage, attempt create/complete with a recorded Idempotency-Key. Expect a bounded API failure, not a hung request. Preserve elapsed time and Hikari connection-timeout/failure metrics. After restoration, retry **the same request body and Idempotency-Key**, then completion for that upload. Verify one upload session, one processing job/outbox row, no duplicate media results and eventual READY. Record actual pool timeout/backoff configuration alongside the result. A blanket HTTP retry of a changed body is not an idempotency test.

## 6. S3 upload succeeds; client never completes

Use a dedicated API session and PUT one object successfully, then intentionally make no completion call. Record the object key/version ID, uploadId, creation time, S3 HEAD expiration header and the applied lifecycle rule. Do not manually delete/abort this sentinel until observation ends. The application also has abandoned-session cleanup, so distinguish its deletion events from S3 Lifecycle events using CloudTrail/S3 events or an independently staged lifecycle sentinel without an API session.

Inspect at intervals using `head-object` and `list-object-versions` for the **exact sentinel prefix**. Record the first observed delete marker/current-version invisibility and later noncurrent-version removal. A one-day lifecycle rule does not mean removal at precisely 24 hours: expiry is asynchronous, and versioned buckets use delete markers before noncurrent-version expiration. Keep this trial `pending observation` until the cloud events/state show the actual actions. Configuration inspection alone does not pass it. [AWS S3 lifecycle behavior](https://docs.aws.amazon.com/AmazonS3/latest/userguide/lifecycle-expire-general-considerations.html).

## Required result record

For each scenario preserve: run UUID, repository/image revision, region/account/deployment identity, start/fault/recovery times, exact resource scope, original settings and restore result, upload/media/job IDs, raw states/log references, expected vs actual behavior, failed attempts, and confidence limits. Check lifecycle cleanup/deletion after the test. Do not publish credentials, signed URLs, private object bodies or sensitive task environment values. Mark an unrun cloud scenario **not executed**, an incomplete observation **pending**, and a failed assertion **failed**; only evidence-backed scenarios may be called verified.
