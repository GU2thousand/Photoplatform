# EKS acceptance and evidence

Acceptance distinguishes source checks, temporary kind execution, real AWS EKS dev, and production. No passing unit test, Helm render or GitHub-hosted kind job establishes AWS Pod Identity, ALB, private networking, RDS TLS, Amazon MQ, S3 or CloudFront behavior.

`scripts/eks_acceptance.py` guards a tagged disposable AWS dev environment and the selected EKS API endpoint/CA, kube-system identity, namespace labels, release revision, image digests and Pod UIDs before adapting the existing compute-independent AWS business suite. It checks that the public ingress host routes to the expected API Service and released Pods. It never substitutes ECS task evidence for Kubernetes evidence. The complete business suite retains failing cases and cleanup failures; its status applies to business cases only. The EKS matrix remains incomplete until separate case evidence is attached.

`scripts/eks_failure.py` deletes exactly one selected media worker Pod using the selected UID precondition after validating the deployment and running durable job. It measures recovery to one DONE job and replacement Ready Pod evidence. Graceful deletion may allow a job to finish; this alone cannot establish SIGKILL, after-S3 interruption, broker/database outage, node drain or ownership of a selected job by a particular Pod. Correlate claim/job logs and run explicit isolated fault cases before marking those rows passed.

`benchmarks/eks_worker_scaling.py` uses `kubectl scale`, real MQ backlog and real upload cohorts. It accepts 1/2/4 workers by default and requires no active worker HPA for a fixed-replica experiment. It saves original replicas before mutation and restores them in `finally`. An initially nonempty queue is rejected to avoid changing other work. Raw upload attempts, terminal outcomes, queue/in-flight backlog, replica/Ready Pod/image evidence, timing and optional read-only database timings remain in the report even on failure. Manual cohorts measure worker capacity; they do not establish an automatic backlog HPA has scaled.

Use `EKS_CLUSTER_NAME`, exact `EKS_CLUSTER_ARN`, `EKS_NAMESPACE`, `EKS_RELEASE`, `EXPECTED_DEPLOY_SHA` (or `DEPLOY_SHA`), `EKS_IMAGE_MANIFEST` pointing to the release image manifest, and `EKS_KUBE_SYSTEM_UID` from release evidence. Provide the existing cloud-harness account, bucket, API, MQ management endpoint and test-token variables. Set `ALLOW_CLOUD_TEST_WRITES=1`, `DISPOSABLE_ENVIRONMENT=true`; mutation harnesses separately require `ALLOW_EKS_FAILURE_INJECTION=1` or `ALLOW_EKS_SCALING=1`. The manual EKS validation workflow holds the same protected environment lock as deployment. No production destructive validation is offered.

For each real run retain `manifest.json` or the release images/cluster records, Helm revision, raw reports, failure timeline, safe logs, Pod/image identities, Terraform plan reference and all failed samples. Include configured cohort counts, task types, image sizes, duration, timeout and known observer load. Missing metrics are unmeasured, never zero. Include API total/success/failure/timeout denominators, p50/p95/p99, DB connection peaks, Pod maximum memory and queue drain duration only when actually measured.

| Required matrix row | Automated evidence available | Additional real evidence required before PASS |
| --- | --- | --- |
| Exact revision and digest rollout | Release Pod imageIDs, revisions and endpoint UIDs | ALB/public origin mapping and live endpoint behavior |
| Upload/private access | Real S3/CORS/checksum/MIME/CloudFront/authorization cases | Complete reported business suite |
| API two replicas / shared JWT / tickets | Two Ready Pods and endpoint identities | Request distribution and per-Pod token/ticket acceptance |
| Cross-Pod WebSocket and reconnect | No complete automated claim | Two users on different Pods, deletion reconnect, permission revocation and REST recovery |
| Repeated events/deletion races | Duplicate completion business case | Duplicate broker delivery, claim/tombstone/object reconciliation |
| Normal rollout/node drain | Rollout image/revision evidence | Shutdown/ACK/job logs, drain timing and no lost work |
| SIGKILL / database session loss | Single Pod graceful deletion harness | Targeted abrupt fault, fencing and post-S3 crash reconciliation |
| MQ / DB interruption | No complete automated claim | Isolated outage/restoration timeline, restart counts and backlog recovery |
| Migration failure | Unit safety gate and bounded Job code | Deliberate dev failing migration with old API and frontend still intact |
| IAM / network isolation | Cluster/namespace identity checks | Actual STS/CloudTrail roles plus denied admin Secret/unrelated prefix and external port checks |
| Capacity / automatic scaling | Fixed 1/2/4 cohort harness | Metric-driven increase/decrease, stale metric behavior and connection budget |
| ML | Opt-in pinned ML image | Model artifact hash, warmup, versions, actual inference/memory and fixed corpus semantic evaluation |
| Application rollback | Runbook | Previous digest/Helm revision, live smoke and migrated schema compatibility |

At implementation time AWS credentials, a reachable EKS dev cluster, production domains and completed cloud matrix are not available in this workspace. Real EKS/managed-service acceptance and production public accessibility remain unverified. Store the actual cloud outcome in a new run-specific report rather than changing this statement based on source or kind success.
