# AWS infrastructure

The `environments/dev` and `environments/prod` roots compose the same application with private S3/OAC, HTTPS ALB, private ECS Fargate services, RDS PostgreSQL 16, Amazon MQ RabbitMQ, ECR, IAM, CloudWatch and GitHub OIDC. This is deployment code, **not evidence that an AWS environment has been provisioned or benchmarked**. Terraform validation and mocked plans cannot prove AWS runtime behavior.

## Prerequisites and state

Use Terraform 1.10+ for the S3 backend's native lock file, AWS credentials obtained through SSO/role assumption, a validated regional ACM certificate covering the API domain, and three supported Availability Zones. The frontend and media use the CloudFront default HTTPS domains; the API uses the supplied domain and regional certificate. Set `route53_zone_id` to create its alias record, or configure the equivalent DNS record separately.

Create an encrypted, versioned, private Terraform state bucket separately from this stack. Copy `backend.hcl.example`, replace its bucket, and initialize with `terraform init -backend-config=backend.hcl`. Each environment has a different state key. Restrict state read/write, require TLS and use an account role; never commit state, tfvars, signing keys or credentials. **Amazon MQ's bootstrap user/password and the external bootstrap secret read are stored in sensitive Terraform state by the provider.** `sensitive` only redacts display. RDS manages its master password without Terraform reading its value. ECS injects application secrets by ARN without Terraform reading their values.

The existing Secrets Manager secrets must use the AWS-managed `aws/secretsmanager` encryption key. Customer-managed KMS keys require a separately reviewed `kms:Decrypt` policy on each applicable task execution role; they are not implicitly granted here. Secrets rotation requires rolling the corresponding ECS tasks because environment injection happens at task startup.

Required external JSON secrets:

| Variable | JSON keys | Purpose |
| --- | --- | --- |
| `application_secret_arn` | `jwt_secret`, `cdn_private_key_pem`, `encoder_token` | Strong JWT/encoder tokens and PKCS8 RSA CloudFront private signing key |
| `mq_secret_arn` | `username`, `password` | Broker bootstrap administrator; password must satisfy the module's constraints |
| `application_database_secret_arn` | `username`, `password` | Pre-created restricted PostgreSQL runtime role; mandatory before prod services start |
| `mq_runtime_secret_arn` | `username`, `password` | Pre-created RabbitMQ application user scoped to the application vhost; mandatory in prod |
| `mq_monitoring_secret_arn` | `username`, `password` | RabbitMQ user with monitoring access; mandatory for the prod collector |

The public CloudFront key supplied as `cdn_public_key_pem` must match the private key in the application secret. Never place the private key in Terraform variables. Generate and upload it through your organization's secret provisioning process. The new AWS default prefix is empty: `staging/<upload>/original`, `media/<id>/v1/...`. **For a migrated database, preserve the old `STORAGE_PREFIX` and copy all referenced object keys, including legacy `originals/` and `thumbnails/`.** API IAM retains those legacy prefixes; media CloudFront only reads `media/`.

## First deployment

1. Copy the selected root's `terraform.tfvars.example`, replace every placeholder, provide the public key, certificate and existing secret ARNs. Use `create_services = false`. Review `terraform plan` and the billable resources before applying. Even bootstrap creates NAT, RDS, MQ and ALB: it incurs costs while ECS is off.
2. Apply the infrastructure. `ecr_repository_urls`, `github_deploy_role_arn`, and other outputs configure the GitHub environment as described in [AWS deployment](../.github/AWS_DEPLOYMENT.md). The OIDC trust matches **one exact repository and environment**, and `aud=sts.amazonaws.com`. Configure the GitHub environment deployment branch policy to allow `main` only and protect production with reviewers; environment subjects do not contain a branch claim. If the account already has GitHub's OIDC provider, set `github_oidc_provider_arn` instead of creating a second provider.
3. Run the build-only GitHub workflow (`deploy_services=false`) so immutable API/worker/collector images exist in ECR. Enable encoder builds when required. Copy the **full `repository@sha256:...` references** from its artifact into `image_refs`, then apply again with services still disabled. This registers executable task definitions and refreshes the reviewed SSM baseline parameters.
4. For a disposable dev environment, `create_services=true` starts two API tasks, one media worker and one backlog collector. Dev defaults use the broker/RDS bootstrap identities to run Flyway. This is not a least-privilege production database setup.
5. Before starting prod services, run the API bootstrap task definition once in the private application subnets using the output task ARN and API security group. While no restricted runtime DB secret is configured, it uses the RDS-managed administrator for Flyway, including `CREATE EXTENSION vector`. Watch its CloudWatch startup/migration logs; stop that standalone task after successful migrations. A task reaching `RUNNING` alone does not prove migration success. Through your private administration path, create a separate PostgreSQL application role and grant only schema usage, table DML and sequence privileges; create scoped RabbitMQ runtime and monitoring users in the current `/` vhost. The runtime user must configure/read/write the media topology (`media.process`, `media.embed`, `media.delete`, retry/dead-letter queues/exchanges) **and** the cross-instance collaboration exchange `photoplatform.team.live` plus ephemeral `photoplatform.team.live.<uuid>` queues; inspect the application declarations before narrowing permission regexes. Populate their external secrets and set the corresponding variables. The regular API then has `SPRING_FLYWAY_ENABLED=false`. Schema changes require a separate controlled migration run before deployment. Do not start prod with the bootstrap identity, and do not disable the guard as a workaround.
6. Enable `create_services=true`, apply, then use the normal deployment workflow for image promotion and frontend publishing. Optional semantic search starts **both** an encoder HTTP service and a separate embedding worker from the ML image. The worker consumes `media.embed`; the encoder explicitly runs `uvicorn`. Initial model downloads and cached model memory are not guaranteed by Terraform: verify image runtime/network access and health-check timing in the target region.

Each service has a Terraform-reviewed SSM task-definition baseline (`task_definition_parameter_names` output). CI reads that exact parameter, preserves the baseline environment, roles, secrets, probes and sidecars, and changes the application image digest. Terraform intentionally ignores service `task_definition` and `desired_count`: CI owns revision promotion, scaling owns capacity. **After changing runtime infrastructure settings, apply Terraform to update the baseline, then run deployment to promote it.** Merely applying a task-definition change does not restart the service. The rollback reference is the actual previous service task definition.

For later production migrations, register/run a standalone copy of the reviewed API task definition under a separately reviewed migration execution role with the RDS administrator secret, explicit `SPRING_FLYWAY_ENABLED=true`, and verified TLS. Do not grant the normal API/worker execution roles administrator secret access. Stop the migration task after successful Flyway logs and before normal rollout. A dedicated migration-only container/exit contract is a remaining operational refinement, not silently implemented here.

## Boundaries and operational behavior

- Public subnets contain ALB/NAT only. API, media worker, encoder, embedding worker and collector receive no public IP. RDS and MQ use isolated data subnets. Security groups permit ALB→API8080, API→encoder8090, API/worker→RDS5432 and MQ5671, collector→MQ443. Application HTTPS egress through NAT supports AWS APIs, image pulls and optional model downloads. An S3 gateway endpoint keeps object traffic off NAT. Dev uses one NAT gateway; prod one per AZ.
- Both buckets block public access, enforce TLS, use encryption and versioning. OAC SigV4 grants each CloudFront distribution only its bucket/prefix. Signed media uses a trusted key group. S3 staging expiration is **one day eligibility**, not an exact 24-hour deletion guarantee: S3 lifecycle evaluation/deletion is asynchronous and noncurrent versions have separate expiry. Media noncurrent versions expire after 30 days; worker deletion explicitly removes versions.
- Media edge cache TTL is bounded to 60 seconds and browser responses use `private, no-store`. An already issued bearer URL can remain usable until its signature/cache expires after deletion or moderation changes; previously downloaded copies cannot be revoked. No claim of instantaneous revocation or propagation is made.
- Worker target tracking is selected by `worker_scaling_mode`: `backlog` (default, target20), `cpu` (target60), or `disabled` for controlled benchmark runs. `BacklogPerTask` counts ready plus in-flight messages divided by **running** ECS tasks; no fabricated zero is published when the broker/ECS snapshot fails. Minimum1 avoids a scale-from-zero deadlock. Missing metrics alarm after three minutes. Disable scaling during 1/2/4/8-task comparison runs, then restore it.
- API/worker ADOT sidecars receive OTLP and scrape private loopback metrics on9091/9100. ECS resource detection adds task identity; metrics export through EMF to `Photoplatform/Application`, traces to X-Ray, logs to CloudWatch. Container Insights supplies CPU/memory. CloudWatch RDS read/write latency is storage I/O, **not SQL query latency**. Preserve the existing Prometheus/Grafana histogram dashboards for query and processing percentiles; EMF histogram export is not a lossless Prometheus replacement. Dashboard/metric existence needs live acceptance evidence.
- The monitoring module creates a dashboard and alarms. Alarms notify only when `alarm_sns_topic_arn` is supplied. This does not create an SNS subscription or guarantee that an operator receives an alert.
- Amazon MQ4.3/m7g is the configurable current default. Region availability and quotas must be checked before applying. Existing older queues/brokers need an explicit upgrade/migration plan; changing a broker bootstrap user can recreate it. The worker uses per-consumer QoS compatible with quorum queues.

## Validation and teardown

Run from the repository:

```sh
terraform fmt -check -recursive infra
terraform -chdir=infra/environments/dev init -backend=false
terraform -chdir=infra/environments/dev validate
terraform -chdir=infra/environments/prod init -backend=false
terraform -chdir=infra/environments/prod validate
terraform -chdir=infra init -backend=false
terraform -chdir=infra test
```

Mocked provider tests exercise bootstrap and service plans, private networking, signed media, browser upload headers, and unsafe startup guards without calling AWS. Follow [failure testing](../docs/failure-testing.md) and the cloud benchmark/acceptance scripts for actual runtime evidence; no capacity/cost/latency result should be inferred from these plans.

Teardown is deliberate. Export required evidence/data, stop traffic, verify the exact AWS account and environment, set `protect_data=false` and apply before destroying. Nonempty buckets and ECR repositories block deletion by default. For disposable dev only, review setting `force_destroy_buckets=true` and applying it before destroy; remove the exact environment's ECR images explicitly. Prod keeps an RDS final snapshot named `<name>-final`; choose a new identifier before deleting a recreated database if that snapshot already exists. Retained snapshots, external secrets, state buckets and CloudWatch data can continue to cost money. `terraform destroy` removes the managed stack only after these protections are deliberately handled; inspect the account afterward. Do not auto-destroy production.

## Sources and verification limits

- [CloudFront OAC and private S3 origins](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-restricting-access-to-s3.html)
- [CloudFront trusted key groups](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-trusted-signers.html)
- [Amazon MQ supported engine versions](https://docs.aws.amazon.com/amazon-mq/latest/developer-guide/rabbitmq-version-management.html)
- [Amazon MQ supported instance types](https://docs.aws.amazon.com/amazon-mq/latest/developer-guide/rmq-broker-instance-types.html)
- [Amazon MQ private network architecture](https://docs.aws.amazon.com/amazon-mq/latest/developer-guide/configuring-private-broker.html)
- [ADOT ECS environment configuration](https://aws-otel.github.io/docs/setup/ecs/config-through-ssm/)
- [ECS task execution role and secrets](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task_execution_IAM_role.html)

The earlier `infra/aws` standalone S3/CDN example is retained only for existing-state migration reference. Do not apply it together with the new environment roots against the same resources. Import or move existing state with a reviewed mapping before adoption; these new resource addresses do not automatically migrate old state.
