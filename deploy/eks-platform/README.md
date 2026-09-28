# EKS platform bootstrap

This installs platform prerequisites into an **already provisioned** dedicated
Photoplatform EKS cluster. Terraform owns the cluster, managed EC2 AL2023 node
groups, network, managed add-ons, IAM roles, Pod Identity associations, access
entries and security groups. This bootstrap owns Helm platform releases and the
namespace release and observability RBAC plus a narrowly scoped namespace
identity reader. Application Helm releases are a separate step.

The checked-in `versions.json` pins Kubernetes 1.36, AWS Load Balancer Controller
chart/application 3.5.0, ASCP 3.1.2 with its bundled Secrets Store CSI Driver 1.6.0,
and metrics-server chart 3.14.0/application 0.9.0. Every downloaded chart is
verified against its official archive SHA-256 **before** Helm runs. The ASCP
release's `Chart.lock` resolves CSI 1.6.0; `helm dependency update` is never run,
because its `^1` declaration could resolve a different release. metrics-server
0.9.x requires Kubernetes 1.34 or later. These are reviewed version pins, not
claims that AWS resources or controllers have been installed.

## Run from the private VPC

Use AWS CLI v2, Python 3.10+, Helm 3, and a kubectl version supported by the EKS
server. The platform operator is the separately managed EKS access entry with
cluster bootstrap privileges. The GitHub application release role must not run
this script. Run from the dedicated VPC runner or another approved host with
private DNS/routing to the cluster. Supply the values reviewed in Terraform:

```sh
python3 deploy/eks-platform/bootstrap.py \
  --cluster photoplatform-eks-dev --environment dev \
  --region us-east-1 --account-id 123456789012 \
  --vpc-id vpc-0123456789abcdef0 \
  --evidence-dir work/eks-platform-evidence/dev
```

The default is a read-only preflight. It verifies the STS caller account, full
cluster ARN, environment-specific Terraform cluster name, VPC, ACTIVE status,
the reviewed Kubernetes version, public API disabled/private API enabled, active
Pod Identity Agent add-on, and the LBC Pod Identity association. It then connects
to the private API using an isolated temporary kubeconfig. It never edits the
caller's current context and does not fall back to a public endpoint.
Flags can also come from `EKS_CLUSTER_NAME`, `AWS_REGION`, `EKS_VPC_ID`,
`EXPECTED_AWS_ACCOUNT_ID`, and `EKS_ENVIRONMENT` (`dev` or `prod`). There are no
implicit account/VPC defaults.

The initial reviewed Terraform apply must use
`network_policy_enforcing_mode = "standard"`. CoreDNS must become ACTIVE before
the Kubernetes bootstrap can install its system policy; starting in strict mode
before that policy exists can block CoreDNS traffic and prevent this sequence
from completing. NetworkPolicy support remains enabled in this initial mode.

After that infrastructure apply, rerun the same command with `--execute`.
This explicitly:

1. Applies the kube-system connectivity NetworkPolicy, namespace with restricted
   Pod Security Admission at v1.36, and the LBC ServiceAccount.
2. Applies the namespace release Role/RoleBinding, two-namespace identity reader
   and three namespace observability Role/RoleBindings. It verifies allowed and
   denied permissions using the publisher group and each workload ServiceAccount.
   The platform operator must have user/group/ServiceAccount impersonation
   privileges for these checks.
3. Applies CRDs from the verified chart packages, including dependencies, so
   upgrades do not rely on Helm's install-only CRD behavior or a mutable URL.
4. Installs/updates LBC, ASCP/CSI and metrics-server with `--atomic --wait` and
   checks the expected Deployment/DaemonSet rollouts.
5. Writes `bootstrap.json` with target identity, versions, results and workload
   statuses. Failure records remain even when a Helm component rolls back.

After bootstrap has installed the kube-system policy and the platform controller
rollouts have passed, the platform owner must make a **second reviewed Terraform
change** setting `network_policy_enforcing_mode = "strict"`. Verify the VPC CNI
configuration and CoreDNS/controller readiness after this change. Application
release is permitted only after strict mode is active; standard mode is an
initial platform bring-up stage, not the application release configuration.

Use `--environment prod --cluster photoplatform-eks-prod` only for the separately
provisioned production cluster. This script supports the commercial AWS partition;
GovCloud/China require their own reviewed policy, image and region configuration.
It contains no `terraform apply/destroy`, application migration, production
ingress cutover, or IAM mutation.

## Controller and secret contracts

Terraform must associate `kube-system/aws-load-balancer-controller` with its LBC
Pod Identity role before bootstrap. The bootstrap creates this ServiceAccount
without an IRSA annotation and installs LBC with `serviceAccount.create=false`,
explicit region/VPC, two controller replicas, and namespace-scoped watching.
Gateway API flags and Service/NLB reconciliation are disabled; this path uses
ALB Ingress. Standard Gateway API CRDs are therefore not needed. The pinned chart
still includes AWS-specific CRDs, applied as platform resources.

The application Ingress must reference the Terraform-owned frontend ALB security
group and use IP targets, HTTPS and its independently reviewed domain/certificate.
LBC does not take ownership of the preexisting ECS ALB or target groups. The EKS
node security group must permit control-plane webhook connections to TCP 9443.
Node groups and workload subnet/security group choices remain in Terraform.
Bootstrap explicitly sets the pinned chart's `enableBackendSecurityGroup=false`
and `enableManageBackendSecurityGroupRules=false`; it verifies the corresponding
rendered controller arguments before Helm installation. The application Ingress
must set `alb.ingress.kubernetes.io/manage-backend-security-group-rules: "false"`
when using its custom Terraform-owned frontend group. Terraform must allow ALB
egress to the target/health-check port and TCP 8080 ingress from that frontend
group to the actual target ENI security group (the node group in this ordinary
VPC CNI path). If Security Groups for Pods are adopted, review the target Pod
security group rules separately. Enabling the Ingress annotation while shared
backend groups are disabled is rejected by LBC.

ASCP installs the CSI Driver from its verified bundled dependency. Secret sync
and automatic rotation are explicitly disabled. Application
`SecretProviderClass` resources must set `parameters.usePodIdentity: "true"`,
use the exact workload ServiceAccount/Pod Identity association, and omit
`secretObjects`. Applications read the read-only CSI files explicitly. Cloud
secret changes require a coordinated rollout; changing a file or environment
variable alone does not establish application reload behavior. No EBS PVC or
cross-AZ writable model cache is introduced.

The node role needs `eks-auth:AssumeRoleForPodIdentity`; private clusters need
access to the `eks-auth` endpoint (NAT or an interface VPC endpoint), Secrets
Manager, ECR/S3 and the other declared dependencies. IRSA's STS endpoint does not
replace the Pod Identity `eks-auth` endpoint. ASCP is a Linux EC2 DaemonSet and this
path does not support Fargate nodes. metrics-server keeps kubelet certificate
verification enabled; fix network/certificate prerequisites instead of adding
`--kubelet-insecure-tls`.

VPC CNI strict policy mode denies ordinary new Pod traffic until the matching
policy is enforced. Install `kube-system-network-policy.yaml` during the initial
standard-mode bootstrap before enabling strict mode. This policy explicitly
allows system namespace traffic for CoreDNS, controller webhooks, AWS APIs, CSI
and kubelet communication. Only trusted platform identities can change workloads
or policies there. Namespace application NetworkPolicies are supplied by the app
chart and do not acquire these system permissions.

## Release permissions

`release-rbac.yaml` grants the EKS access-entry group `photoplatform-deployers`
only the resource types required by the app Helm release in
`photoplatform-dev` or `photoplatform-prod`. Helm needs namespace Secret access
for release metadata; cloud credentials remain CSI files. Publish identities
can change namespace workloads and must be protected accordingly. This Role
does not permit RBAC creation, `bind`, `escalate`, cluster resources, nodes,
direct Pod creation/exec, or access to kube-system secrets. A separate,
bootstrap-owned ClusterRole/ClusterRoleBinding grants this group only `get` on
the **two exact Namespace objects** `[target namespace, kube-system]`, permitting
the release guard's label/UID checks. It grants no namespace list/watch, other
namespace reads, cluster resource writes or additional cluster resource access.
The generated files are `namespace-read-rbac.json` and `observability-rbac.json`.

Bootstrap also provisions these namespace Roles/RoleBindings for the fixed
chart-owned ServiceAccounts, without granting them Secret access or API writes:

| ServiceAccount | Read permissions in the target namespace |
| --- | --- |
| `photoplatform-queue-collector` | Pod `list` |
| `photoplatform-prometheus` | Pod `get`, `list`, `watch` |
| `photoplatform-kube-state-metrics` | Pod, Deployment and HPA `list`, `watch` |

The application chart owns those ServiceAccounts and requires
`rbacProvisioned: true` before the optional components are enabled; it must not
render Roles or RoleBindings. Bindings can be provisioned before their
ServiceAccounts exist. Bootstrap checks their permissions through impersonation,
which [maps the ServiceAccount username and groups](https://github.com/kubernetes/kubernetes/blob/v1.36.0/staging/src/k8s.io/apiserver/pkg/endpoints/filters/impersonation/impersonation.go)
without issuing a ServiceAccount token or creating application identities.

Namespace failure
injection that requires deleting/executing Pods and node-drain experiments must
use a separately authorized operator identity. Do not give the app publisher
cluster-admin to run acceptance experiments.

## Local verification

```sh
python3 -m unittest discover -s deploy/eks-platform/tests -v
python3 deploy/eks-platform/bootstrap.py \
  --cluster photoplatform-eks-dev --region us-east-1 \
  --account-id 123456789012 --vpc-id vpc-0123456789abcdef0 \
  --render-dir work/eks-platform-render \
  --evidence-dir work/eks-platform-render-evidence
```

Rendering downloads/verifies the same pinned charts, invokes Helm with Kubernetes
1.36 capabilities and makes **no AWS/Kubernetes API calls**. Chart-generated
webhook TLS material can appear in render files; these stay in the ignored work
directory with restricted permissions and must not be committed/uploaded. Local
rendering and safety tests do not establish IAM operation, successful secret
mounts, webhook networking, actual CSI/metrics readiness or an online deployment.
Cloud acceptance must retain Pod imageIDs, dependency/IAM negative checks and
real HTTPS business results in the release evidence.

## Official source pins

- [LBC v3.5.0 IAM policy](https://github.com/kubernetes-sigs/aws-load-balancer-controller/blob/v3.5.0/docs/install/iam_policy.json)
  is vendored byte-for-byte under `infra/modules/eks/policies/`. Its upstream
  tag conditions are retained. Discovery APIs require wildcard resources;
  the dedicated Pod Identity trust is constrained by Terraform to this cluster
  and ServiceAccount. Future policy refinements must be tested against the
  controller version and must update the recorded hash.
- [AWS EKS Helm install instructions](https://docs.aws.amazon.com/eks/latest/userguide/lbc-helm.html)
  document the region/VPC and externally created ServiceAccount interface.
- [LBC v3.5.0 Gateway feature flags](https://github.com/kubernetes-sigs/aws-load-balancer-controller/blob/v3.5.0/docs/guide/gateway/gateway.md)
  document the disabled ingress-only Gateway dependency.
- [ASCP 3.1.2 release](https://github.com/aws/secrets-store-csi-driver-provider-aws/releases/tag/3.1.2)
  and [Pod Identity integration](https://docs.aws.amazon.com/secretsmanager/latest/userguide/ascp-pod-identity-integration.html)
  define provider/identity configuration.
- [EKS private cluster requirements](https://docs.aws.amazon.com/eks/latest/userguide/private-clusters.html)
  and [Pod Identity Agent setup](https://docs.aws.amazon.com/eks/latest/userguide/pod-id-agent-setup.html)
  define private AWS endpoint and node IAM prerequisites.
- [metrics-server v0.9.0 compatibility matrix](https://github.com/kubernetes-sigs/metrics-server/blob/v0.9.0/README.md)
  defines the Kubernetes minimum version.
- [CSI secret rotation semantics](https://secrets-store-csi-driver.sigs.k8s.io/topics/secret-auto-rotation)
  describe application reread/restart responsibilities.
