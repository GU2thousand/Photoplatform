# Terraform state ownership and migration

No state address is moved in this implementation. EKS is an independent compute root that explicitly references shared resources; this is the plan's safe temporary ECR ownership option. Do not remove `module.ecs`, set `count=0` on it, or replace the original root with the EKS root. `create_services=false` also does not retire already deployed services safely unless its plan is separately reviewed. ECR repositories and lifecycle policies remain in that ECS module's state even when future ECS compute is intentionally retired.

| Resource in an original environment state | Address retained | EKS root behavior |
| --- | --- | --- |
| VPC, subnets, routes and SG resources | `module.platform.module.network.*` | Read-only IDs/ARNs; no import |
| Database | `module.platform.module.rds.*` | TLS host and existing SG ID |
| RabbitMQ | `module.platform.module.mq.*` | TLS host and existing SG ID |
| S3 | `module.platform.module.s3.*` | Exact bucket/prefix references |
| CloudFront | `module.platform.module.cloudfront.*` | Exact distribution references |
| ECS ALB/DNS | `module.platform.module.alb.*`, `module.platform.aws_route53_record.api` | No changes; separate EKS controller ingress |
| API ECR/lifecycle | `module.platform.module.ecs.aws_ecr_repository.workloads["api"]`, `module.platform.module.ecs.aws_ecr_lifecycle_policy.workloads["api"]` | Existing ARN, immutable digest images |
| Worker ECR/lifecycle | Same addresses with `["worker"]` | Existing ARN |
| Encoder ECR/lifecycle | Same addresses with `["encoder"]` | Existing ARN |
| Collector ECR/lifecycle | Same addresses with `["collector"]` | Existing ARN |
| GitHub account OIDC provider | `module.platform.aws_iam_openid_connect_provider.github[0]` when created here | Reference existing ARN; no duplicate provider |

If applying directly from `infra/` rather than an environment wrapper, omit the `module.platform.` prefix. Use actual `terraform state list` results before any future move; the deployed state may differ from the repository example. `infra/aws` is a legacy S3/CDN example and must not be applied over these shared resources.

The new state creates only `module.eks.*`: EKS cluster/node groups/add-ons/access/Pod Identity, EC2 launch template, IAM roles/policies, three dedicated SGs, explicit SG rules, KMS cluster key/alias and cluster log group. The dependency SGs stay in the original state; only the three rules from the EKS node SG are EKS owned. The EKS ALB itself is not in Terraform state: LBC owns it via the application Ingress. Delete its ingress/ALB first on retirement, retain its evidence, then review compute teardown. Removing a Terraform SG while a controller-owned ALB still uses it can fail.

Create a distinct backend key for `eks-dev` and `eks-prod` using the provided examples. The existing state bucket must already have versioning, encryption, least-privilege access and locking. Never use an EKS root with the original `dev`/`prod` backend key. This module reads no remote state and no secret versions.

```sh
terraform -chdir=infra/environments/eks-dev init -backend-config=/secure/path/eks-dev.backend.hcl
terraform -chdir=infra/environments/eks-dev validate
terraform -chdir=infra/environments/eks-dev plan -var-file=/secure/path/eks-dev.tfvars -out=/secure/path/eks-dev.tfplan
terraform -chdir=infra/environments/eks-dev show -json /secure/path/eks-dev.tfplan
```

Review the intended account/region/state key, all physical IDs, new cost/capacity and resource changes. Reject any change to preexisting data/ECR/ECS resources, any unanticipated destroy/replace, wrong environment/network/Secret ARN, public Kubernetes API, public DB/MQ, `latest` or bootstrap/admin credentials assigned to workload roles. Plan output can contain sensitive infrastructure metadata; save it with access controls. A local mocked plan is not a reviewed plan against the real state.

Initial provisioning keeps CNI policy enforcement in `standard` mode with policy support enabled, because CoreDNS must reach the Kubernetes API before a system namespace policy exists. After the administrator executes the pinned platform bootstrap, set `network_policy_enforcing_mode="strict"`, review a second plan (only the add-on configuration should change), and apply it before application release. This avoids a bootstrap deadlock while ensuring app Pods begin with enforced policies. The release preflight rejects a non-strict/missing CNI configuration.

If ECR is extracted from ECS later, do it within the **existing** state in a dedicated change. The exact proposed per-instance map is:

| Old address | Future address, only after a shared registry module is implemented |
| --- | --- |
| `module.platform.module.ecs.aws_ecr_repository.workloads["api"]` | `module.platform.module.registry.aws_ecr_repository.workloads["api"]` |
| Same repository address `["worker"]` | Same registry repository address `["worker"]` |
| Same repository address `["encoder"]` | Same registry repository address `["encoder"]` |
| Same repository address `["collector"]` | Same registry repository address `["collector"]` |
| `module.platform.module.ecs.aws_ecr_lifecycle_policy.workloads["api"]` | `module.platform.module.registry.aws_ecr_lifecycle_policy.workloads["api"]` |
| Same lifecycle address `["worker"]` | Same registry lifecycle address `["worker"]` |
| Same lifecycle address `["encoder"]` | Same registry lifecycle address `["encoder"]` |
| Same lifecycle address `["collector"]` | Same registry lifecycle address `["collector"]` |

That future change needs one `moved` block per listed address, the implemented destination module, a backed-up state and a plan showing zero destroy/create for the physical repositories. These mappings are documentation, not active blocks: their destination module does not yet exist. Never import the same repository into the EKS state while leaving it in the original state. Moving between state files would require a separate explicit remove/import transaction, locking both roots and restoring from backups if it fails; it is unnecessary for this route.

Retirement preserves shared data, ECR and audit artifacts. Production EKS deletion protection and cluster KMS `prevent_destroy` require an explicit reviewed retirement change; there is no unattended compute cleanup command in the app release workflow. Application Helm rollback does not reverse database migrations or alter infrastructure ownership.
