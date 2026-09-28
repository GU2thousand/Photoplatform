# Independent EKS compute module

Creates private EKS 1.36 managed AL2023 CPU node groups, encryption/logs, pinned managed add-ons, Pod Identity roles, access entries and dedicated security groups. Does not manage ECR, VPC/subnets/routes, data services, frontend/CDN, DNS or an ALB resource. Only added SG grants into the existing DB/MQ groups are owned here.

Use `infra/environments/eks-dev` or `eks-prod`; do not call this from the existing all-in-one `infra/main.tf` or remove its ECS module. Read `docs/kubernetes/terraform-state-migration.md` and fill account inventory first. All regional add-on builds and the AL2023 release are required exact inputs; the examples deliberately use invalid placeholder pins to prevent an accidental moving-version deployment.

Initial bootstrap uses standard CNI enforcement with policy support enabled. Apply the pinned platform bootstrap's kube-system policy, then review/apply the add-on change to strict mode before any app release. Runtime namespaces use Helm policy, restricted workload identities and private TLS dependencies.

Local checks:

```sh
terraform -chdir=infra/modules/eks init -backend=false
terraform -chdir=infra/modules/eks validate
terraform -chdir=infra/modules/eks test
terraform -chdir=infra/environments/eks-dev init -backend=false
terraform -chdir=infra/environments/eks-dev validate
terraform -chdir=infra/environments/eks-prod init -backend=false
terraform -chdir=infra/environments/eks-prod validate
```

Tests use `mock_provider "aws"`, only plan, and cover private API/IMDS/identity/resource isolation plus unsafe account, AZ, Secret, prefix, version, disposable-production and release-admin configurations. Fixture AMI/add-on strings validate syntax only and must never be taken as regional version evidence. No test provisions an AWS resource or validates real IAM/CNI/TLS behavior.
