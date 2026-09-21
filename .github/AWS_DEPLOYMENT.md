# AWS delivery and cloud validation

`Verify` remains credential-free: backend tests, frontend build/tests/audit, worker and collector tests, deployment safety tests, Terraform formatting and `init -backend=false` / `validate` for **both dev and prod**, credential-free root-module `terraform test` with mocked AWS covering private access/topology/production configuration safeguards, and a disposable Compose integration pipeline. The integration broker uses RabbitMQ 4.3.6 to exercise the Amazon MQ target's protocol restrictions. Its tiny exact/HNSW exercise verifies that the benchmark runs; it is not a cloud performance measurement.

`Deploy AWS` publishes only a **successful Verify run for the exact current main commit**. A `workflow_run` event is independently checked against the repository's `ci.yml` workflow ID, repository, branch, event type, SHA and conclusion using GitHub's API. The checkout is pinned to that SHA. No artifacts or executable code from a pull request are consumed by the privileged workflow. Older queued revisions are refused; rerun Verify for current main if necessary.

The workflow requests OIDC credentials only in a protected environment job. It uses no `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` repository secrets. Automatic dev deployment is disabled until the repository variable `AUTO_DEPLOY_DEV=true` is set. Prod is manual. A missing AWS configuration has no effect on normal Verify runs or main pushes when automatic deployment is disabled.

## Provision and bootstrap

1. Configure an AWS account, Terraform remote state with encryption/locking, DNS and ACM certificates according to [the infrastructure guide](../infra/README.md). Use distinct dev/prod state and resources. A production deployment needs an HTTPS API origin accessible to browsers; the deployment preflight refuses plain HTTP.
2. Apply the environment with `create_services=false`. This creates infrastructure, ECR repositories and IAM roles before any application image exists. Do not put placeholders such as `latest` into running services.
3. Create GitHub environments `dev` and `prod`, restrict deployment branches to `main`, and configure required reviewers for prod. The IAM trust policy must match this repository and `repo:GU2thousand/Photoplatform:environment:dev` or `...:prod`, with OIDC audience `sts.amazonaws.com`.
4. Transfer the nonsensitive Terraform outputs into environment variables below. Supply values from the chosen environment; do not mix dev/prod resources. Keep all application credentials in AWS Secrets Manager. Its secret ARNs in task definitions are references, not secret values.
5. Merge a successfully verified revision to main. Run **Deploy AWS** for dev with **deploy_services=false**. This builds and pushes SHA-tagged API, worker, collector and optional ML images, then uploads `images.json` with their immutable ECR digests. It does not require an existing ECS service, API URL or frontend URL.
6. Set Terraform `image_refs` to the `images` map of `repository@sha256:...` values from that run, enable `create_services=true`, and apply. The encoder and embedding worker use the same ML image. Wait for initial services to become healthy; read Terraform's own plan before applying.
7. Run **Deploy AWS** again with **deploy_services=true**. After successful end-to-end delivery, opt into automatic dev deployments if desired. Do not set `AUTO_DEPLOY_DEV=true` during bootstrap.

For example, the image-only dispatch after configuration is:

```bash
gh workflow run deploy-aws.yml --ref main -f environment=dev -f deploy_services=false
```

Read the run and its `aws-deployment-<SHA>` artifact before setting initial Terraform image values. A failed or partial image build is not an application deployment. The repository's workflow does not run `terraform apply` or `terraform destroy` automatically.

## GitHub environment variables

| Variable | Value / source |
| --- | --- |
| `AWS_ROLE_ARN` | Terraform `github_deploy_role_arn` |
| `AWS_REGION` | Terraform `region` |
| `AWS_ACCOUNT_ID` | The expected 12-digit AWS account ID; enforced before mutation |
| `ECS_CLUSTER` | Terraform `ecs_cluster_name` |
| `TASK_DEFINITION_PARAMETERS` | JSON map from Terraform `task_definition_parameter_names`; points to reviewed task definition ARNs in SSM |
| `ECS_API_SERVICE` | Terraform `api_service_name` |
| `ECS_WORKER_SERVICE` | Terraform `worker_service_name` |
| `ECS_COLLECTOR_SERVICE` | Terraform `collector_service_name` |
| `ECS_ENCODER_SERVICE` | Terraform `encoder_service_name`, when semantic search is enabled |
| `ECS_EMBEDDING_WORKER_SERVICE` | Terraform `embedding_worker_service_name`, when semantic search is enabled |
| `ECR_API_REPOSITORY` | Terraform `ecr_repository_urls.api`, no tag |
| `ECR_WORKER_REPOSITORY` | Terraform `ecr_repository_urls.worker`, no tag |
| `ECR_COLLECTOR_REPOSITORY` | Terraform `ecr_repository_urls.collector`, no tag |
| `ECR_ENCODER_REPOSITORY` | Terraform `ecr_repository_urls.encoder`, when enabled, no tag |
| `ENABLE_ENCODER` | `true` only when encoder and embedding-worker services are configured |
| `FRONTEND_BUCKET` | Terraform `frontend_bucket_name` |
| `FRONTEND_DISTRIBUTION_ID` | Terraform `frontend_distribution_id` |
| `FRONTEND_URL` | Terraform `frontend_url`, an HTTPS origin |
| `API_BASE_URL` | Terraform `api_base_url`, an HTTPS origin; compiled as `VITE_API_BASE_URL` |

Set `AUTO_DEPLOY_DEV` at **repository** scope because the source-verification job intentionally has no environment or AWS credentials. Everything else belongs to its own environment. GitHub variables are appropriate for resource names/ARNs, not passwords.

The deploy role needs ECR push/pull and `DescribeImages` on the designated repositories, ECS describe/register/update and `TagResource`, `iam:PassRole` only for the application's execution/task roles, frontend bucket list/write access, CloudFront create/get invalidation, and `ssm:GetParameters` on its Terraform baseline parameters. Terraform provides the resource-scoped role. `RegisterTaskDefinition` and ECR token issuance require the AWS-supported wildcard resource scope; neither grants application data access.

## Rolling release evidence and failure handling

Images are tagged with the verified full Git SHA and deployed by digest. Immutable ECR repositories reject tag replacement; reruns reuse a previously pushed SHA's digest. The helper checks that the credentials and every ECR repository belong to the expected account and region.

Each service receives a new revision based on the **exact task definition ARN in its Terraform-managed SSM String parameter**, preserving IAM roles, secret references, resource limits and the OpenTelemetry sidecar. Apply reviewed Terraform configuration changes before deploying: although Terraform deliberately ignores service `task_definition` drift, its SSM parameter advances to the reviewed configuration revision. The helper reads only these scoped parameters, validates the account/region and verifies that each ACTIVE baseline belongs to the service's current family. It records the currently running revision, baseline and newly deployed revision. Changes to environment variables, secrets, roles or CPU therefore reach the next release without trusting an arbitrary latest family revision. Do not apply Terraform concurrently with a deployment. App container names are stable (`api`, `worker`, `encoder`, `embedding-worker`, `collector`). Updates use 100% minimum healthy capacity, 200% maximum capacity, and the ECS circuit breaker with rollback. After `services-stable`, the helper also checks that **the requested revision** is PRIMARY/COMPLETED and the desired positive task count is running. Stability caused by a rollback is reported as failure.

The frontend is built with the public HTTPS API origin, uploaded after service rollout succeeds, and its CloudFront cache is invalidated. Existing hashed JS/CSS objects remain available for tabs already open during deployment. `index.html` is revalidated; after invalidation the exact downloaded bytes must match this release, and the external API `/readyz` must return 200. Artifacts record image digests, prior/requested task revisions, rollout state, frontend index SHA-256 and invalidation ID. No task definitions, secrets or signed media URLs are uploaded.

A release across multiple services is not one atomic transaction. If one service fails, its ECS circuit breaker can roll it back while another service has already advanced. Inspect `rollout.json`, CloudWatch and service events; correct the release or restore the recorded prior revisions. Frontend publication is withheld unless every requested service revision passes. Frontend publication failure is separately reported and does not automatically roll back APIs. Database changes must therefore remain backward compatible across rolling versions.

## Manual cloud acceptance and benchmarks

**Cloud acceptance and benchmarks** is a separate main-only manual workflow for dev stacks. The harness also verifies the actual media bucket has `Environment=dev` and `DisposableEnvironment=true` tags. Choose `acceptance`, `uploads` (default concurrent users 20/50/100), or `scaling` (default tasks 1/2/4/8), then explicitly check `confirm_disposable`. It shares the deployment concurrency group to avoid deploying into an active experiment. The workflow refuses environments without `DISPOSABLE_ENVIRONMENT=true` and never reuses the deployment role for test writes.

Additional environment configuration:

| Variable / secret | Purpose |
| --- | --- |
| Variable `AWS_TEST_ROLE_ARN` | Separate OIDC role for the requested cloud experiment with `MaxSessionDuration >= 7200`; grant only needed actions on the disposable stack |
| Variable `DISPOSABLE_ENVIRONMENT` | Must be the literal `true`; production datasets are not acceptable test targets |
| Variable `S3_MEDIA_BUCKET` | Terraform `storage_bucket`, the private media bucket name |
| Variable `CLOUDFRONT_MEDIA_DOMAIN` | Terraform `cdn_domain`, the CloudFront media distribution hostname |
| Variable `STORAGE_PREFIX` | Terraform `storage_prefix`, matching the API and worker |
| Secrets `TEST_OWNER_TOKEN`, `TEST_OTHER_TOKEN`, `TEST_ADMIN_TOKEN` | Distinct pre-provisioned test account bearer tokens; acceptance needs all three |
| Variable `RABBITMQ_MANAGEMENT_URL` | Terraform `mq_management_url`, accessible from the private runner, for scaling only |
| Secrets `RABBITMQ_USERNAME`, `RABBITMQ_PASSWORD` | Least-privilege monitoring credentials for the disposable broker, for scaling only |
| Variable `CLOUD_TEST_RUNNER` | JSON labels, e.g. `["self-hosted","linux","photoplatform-vpc"]`, for scaling only |

Acceptance checks real Amazon S3 and CloudFront, including browser-origin CORS preflight using `FRONTEND_URL`. Upload tests require the owner token. Scaling suspends and restores Application Auto Scaling; its role needs the harness's ECS, Application Auto Scaling and CloudWatch reads/writes on the chosen worker service. Use a short-lived self-hosted runner within the VPC to reach private Amazon MQ. Do not make MQ/RDS public merely to run tests, and do not register this runner for untrusted pull-request jobs. Public-endpoint acceptance/upload suites use GitHub-hosted runners.

The explicitly configured test role session lasts two hours and the job is bounded to 110 minutes. For longer research runs, run the harness from an authorized VPC job with a renewable task role. If a runner disappears or a job is forcibly terminated, inspect worker desired count and restore autoscaling using the experiment evidence before another test. Every run uploads raw `cloud-results/` evidence with `if: always()`, including incomplete/failed results. An empty or failed result does not establish an acceptance criterion, throughput claim or cloud deployment. Run pgvector scale comparisons from an authorized VPC environment following the benchmark guide; the tiny CI smoke is separate evidence.

Failure injection is deliberately absent from automatic deployment. Follow [failure testing](../docs/failure-testing.md) against a disposable stack after baseline cloud acceptance passes.

## References

- [GitHub: secure use of workflow_run](https://docs.github.com/en/actions/reference/security/secure-use)
- [GitHub: OIDC with AWS](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws)
- [AWS: ECS deployment circuit breaker and rollback](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/deployment-circuit-breaker.html)
