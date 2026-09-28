# EKS dev compute root

This is an independent compute state, not a replacement for `infra/environments/dev`. Existing network/data/ECR/frontend resources keep their original single owner. Read [prerequisites](../../../docs/kubernetes/prerequisites.md), [state ownership](../../../docs/kubernetes/terraform-state-migration.md), and [identity](../../../docs/kubernetes/secrets-and-iam.md).

Copy `backend.hcl.example` and `terraform.tfvars.example` outside Git and replace inventory IDs/ARNs and mandatory regional add-on/AL2023 pins. Use a unique backend key and approved AWS account. The provider rejects the wrong account; the module rejects public-address node subnets, a single AZ, shared migrator/admin Secrets and invalid version pins. Backend creation and provisioning are separate platform operations.

1. Review a real plan using the dedicated backend and selected inventory. It must contain only EKS compute/IAM/log/KMS/SG resources and three grants into the existing data SGs. Retain a safe evidence copy. Do not run it over the existing ECS state key.
2. Provision initial compute with `network_policy_enforcing_mode="standard"`; no application is deployed yet. CoreDNS must become ACTIVE before strict policy mode is enabled.
3. From the private VPC runner, execute [platform bootstrap](../../../deploy/eks-platform/README.md) as the named admin role. It installs system policy, pinned controllers/CSI/metrics, the namespace and release RBAC.
4. Set `network_policy_enforcing_mode="strict"`, review a second plan changing only the CNI configuration, apply, and verify aws-node/CNI readiness. App release preflight must reject standard mode.
5. Use namespace `photoplatform-dev`, cluster `photoplatform-eks-dev`, Helm release `photoplatform`, and GitHub protected environment `eks-dev`. Feed `alb_security_group_id` and `public_subnet_ids` into the Ingress. The ALB/hostname must be independent of the existing ECS endpoint.
6. Run migration, new-digest rollout and live acceptance with the separately authorized release/operator identities. Only documented real AWS evidence establishes online operation.

`disposable_environment` defaults false. Only a dedicated dev environment may set it true for fault tests; an operator also labels the namespace disposable after verifying all dependencies are disposable. Production is never disposable and deletion protection is enabled. The cluster KMS key is retained unless a separate reviewed retirement change permits removal.

Repository format/validate/mock-plan checks are implemented. Actual account inventory, reviewed AWS plans, regional compatibility, private-runner access and deployment have not been performed by these fixtures.
