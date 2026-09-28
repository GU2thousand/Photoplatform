# EKS application release

This repository supplies a separate EKS application route. It does not provision AWS from CI. First provision the reviewed independent dev compute state, preserve the existing owners of RDS, MQ, S3, CloudFront and ECR, and configure Pod Identity, Secrets Store CSI and the ALB controller. The deploy job never calls Terraform.

`Deploy EKS` is manual. It accepts only the current canonical `main` SHA with a successful `Verify` run for that exact SHA. The script checks source and Verify again before each stage that changes an environment, including after protected-environment approval. A new main commit invalidates a pending old release. PR success, a successful older main build, an image bearing an arbitrary SHA tag, or an old Ready Pod cannot satisfy this gate.

Create protected GitHub environments `eks-dev` and `eks-prod`, with required reviewers for prod and deployment branches limited to main. OIDC roles must trust the specific environment subject, repository and account. The default runner labels are `["self-hosted","linux","eks-deployer"]`; override `EKS_RUNNER_LABELS` only with a reviewed JSON label array for a runner that reaches the private API endpoint. Public GitHub runners are suitable for temporary kind verification, but do not automatically reach a private EKS control plane.

The runner needs AWS CLI v2, compatible kubectl, Helm 3, Docker/buildx, Python 3.12 or newer, and a reviewed egress path for ECR, GitHub, dependencies and CloudFront. Pin actual installed tool versions in the resource inventory. A deploy runner must be dedicated and must never execute untrusted PR jobs. ECR repositories require immutable tags without mutable exclusions.

Configure the following environment variables through GitHub environment variables, with fixture bearer tokens in environment secrets:

| Variables | Purpose |
| --- | --- |
| `AWS_REGION`, `AWS_ACCOUNT_ID`, `AWS_ROLE_ARN` | Explicit AWS account and OIDC role |
| `EKS_CLUSTER_NAME`, `EKS_CLUSTER_ARN`, `EKS_NAMESPACE`, `EKS_RELEASE` | Existing cluster ARN, exact `photoplatform-dev` or `photoplatform-prod` boundary, fixed release `photoplatform` matching provisioned Pod Identity service accounts |
| `ECR_API_REPOSITORY`, `ECR_WORKER_REPOSITORY` | Immutable repositories in that account and region |
| `ENABLE_ENCODER`, `ECR_ENCODER_REPOSITORY` | Opt-in real ML image |
| `ECR_COLLECTOR_REPOSITORY` | Required when queue collector is enabled |
| `EKS_HELM_VALUES_JSON` | Non-secret JSON overlay: endpoint hosts, CIDRs, Secret ARNs, limits and ingress; no passwords or signing keys |
| `FRONTEND_BUCKET`, `FRONTEND_DISTRIBUTION_ID`, `FRONTEND_URL`, `API_BASE_URL` | Existing S3/CloudFront frontend and HTTPS ALB API origin |
| `S3_BUCKET`, `STORAGE_PREFIX`, `CLOUDFRONT_DOMAIN` | Real media bucket/prefix and signed media delivery domain |
| `TEST_OWNER_TOKEN`, `TEST_OTHER_TOKEN`, `TEST_ADMIN_TOKEN` | Three distinct pre-provisioned fixture accounts; secrets, never printed |

`EKS_HELM_VALUES_JSON` may set `config`, `secrets`, `ingress`, `network`, `capacity`, workload limits, `telemetry` and `queueCollector`. It may provide only pinned external `images.prometheus`, `images.otel` and `images.kubeStateMetrics`; the script controls application images, AWS identity, environment, revision, migration and ML enablement. Its SHA-bearing release overlay is rendered against the committed `values-dev.yaml` or `values-prod.yaml`. Region, account and cluster are injected from the protected environment. Each enabled role's `secrets.<role>.versionId` pins the immutable Secrets Manager version. Do not put secret contents in a variable or Helm values. Dev replica acceptance additionally uses `api.exposeInstanceId=true` to identify responses; production must keep it false.

The pre-created namespace must carry `app.kubernetes.io/part-of=photoplatform` and `photoplatform.io/environment=dev|prod`. Disposable dev validation additionally requires `photoplatform.io/disposable=true`; the dev media bucket requires `Project=photoplatform`, `Environment=dev`, `DisposableEnvironment=true`. Cluster tags must match `Project=photoplatform` and the selected environment. Bootstrap owns RBAC; the application role gets namespace release permissions and a narrowly restricted cluster read grant permits `get` of only the target namespace and `kube-system` for identity evidence. The release role cannot create/patch RBAC and must not receive cluster-admin. Manual fault acceptance uses a separate `EKS_VALIDATION_ROLE_ARN` with explicitly reviewed namespace exec/port-forward/policy/rehearsal permissions.

The release order is:

1. Validate configuration and actual AWS/cluster/namespace identity. Require private-only cluster management, active pinned CNI/DNS/proxy/Pod Identity addons and CNI NetworkPolicy enabled in strict mode. Save cluster ARN, version, API endpoint/CA hash, addon versions, namespace UID and kube-system UID.
2. Export each Docker build context with `git archive` from the exact verified commit, excluding untracked/ignored runner files; save its hash. Build fresh `SHA-run-attempt` immutable image tags with SBOM and BuildKit provenance; compare ECR digest, inspect the pushed OCI revision label, and record runnable linux/amd64 manifest digests. Every application image is released by digest.
3. Build the frontend locally without publishing it.
4. Render only dedicated migrator ServiceAccount, SecretProviderClass and Job. Apply identity/secret references, then create a unique Job. Require `/app/migrate.sh`, `restartPolicy: Never`, deadline at most 900 seconds and backoff at most one. Wait for this exact Job UID to complete, preserving safe logs before TTL cleanup.
5. Only after migration succeeds, run `helm upgrade --install --atomic --wait` with migration disabled. Require the requested template revision, observed generation, exact updated/available replica count, new Ready Pod UIDs and matching imageIDs, including the OCI architecture leaf digest. Require EndpointSlices to target exactly those API Pod UIDs. Wait at most five minutes for the dedicated controller-owned ALB, public DNS and healthy business-port targets to match the released endpoint IPs. Check public HTTPS readiness and dev response identity when enabled.
6. Run real AWS upload/checksum/MIME/CORS, duplicate completion and private authorization/CloudFront fixture checks, then request fixture cleanup. Preserve all failures and denominators. A failed migration, rollout or business check stops frontend publication.
7. Publish frontend assets then index, invalidate CloudFront and verify exact served index bytes and API readiness. Preserve release/failure evidence.

The artifact contains safe JSON evidence, including SHA, Verify run ID, image digests/provenance, migration Job UID/status/logs, Helm revision, live Pod imageIDs, API endpoint targets and business cases. It excludes kubeconfig and fixture bearer tokens. Review logs for accidental sensitive application output; do not add secrets to application exception messages.

The `photoplatform-eks-eks-dev|eks-prod` concurrency group is shared by deployment and manual EKS acceptance/failure/scaling workflows. Do not run independent CLI mutations outside this lock while CI is operating. An application rollback restores chart/image versions only; expand-contract database compatibility is required. No script performs destructive down migrations or infrastructure rollback.

The initial route is dev. Production release additionally refuses to run until a separate cutover decision sets `EKS_PRODUCTION_CUTOVER_APPROVED=1`; fixture smoke requires `ALLOW_PRODUCTION_SMOKE=1`. These variables are gates, not evidence of user approval. An operator must review the completed dev matrix, production resource identity, backups and rehearsed rollback before configuring them. The current implementation does not claim an AWS deployment or production cutover has happened.
