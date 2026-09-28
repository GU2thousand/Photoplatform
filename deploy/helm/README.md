# Photoplatform Helm compute route

The chart deploys compute only; PostgreSQL/pgvector, RabbitMQ, S3, CloudFront,
cluster add-ons and platform RBAC keep their existing single infrastructure owner.
Use the EKS release workflow rather than installing directly in production.

`values-dev.yaml` and `values-prod.yaml` are overlays, not complete deployment
inventories. Supply account, region, cluster, actual dependency hosts and allowed
subnet CIDRs, ACM certificate, per-role Secret ARNs and immutable Secret version
IDs, full verified main commit, and images by digest. AWS release name is fixed
to `photoplatform` and namespace to `photoplatform-dev` or `photoplatform-prod`;
these names match Terraform Pod Identity associations. Namespace creation stays
with bootstrap, with `app.kubernetes.io/part-of=photoplatform` and
`photoplatform.io/environment=dev|prod` labels.

Runtime config contains no credentials. Each enabled workload has its own
ServiceAccount and CSI mount. `KEY_FILE=/mnt/secrets/KEY` is explicitly passed to
the image entrypoint; mounting a file by itself does not create an environment
variable. ASCP mounts JSON keys as read-only `0444` files in the single-container
Pod, making nonroot reads independent of optional CSI fsGroup support. Pinning
`objectVersion` and putting it on Pod annotations causes a uniform rollout when
the reviewed Secret version changes. JWT uses one signing key: changing it
invalidates old tokens/tickets; there is no key overlap guarantee. Secret rotation
needs a coordinated rollout and fresh login/tickets. Never persist Secret bodies
in Helm values, logs or evidence.

AWS profiles are always `aws,eks`; Java and worker startup guards verify actual
Secret contents and transport contracts. In particular, Helm cannot inspect an
external JWT Secret to detect a demo value; Java rejects it during startup and
the release cannot complete a rollout. The independent `values-local.yaml`
uses `kubernetes-local`, existing namespace Secrets, local dependency Pods and
MinIO. Local tests prove neither IAM nor ALB/TLS/CloudFront behavior.

Run migration before any release update:

```sh
helm template photoplatform deploy/helm/photoplatform \
  --namespace photoplatform-dev -f deploy/helm/values-dev.yaml -f inventory.json \
  --show-only templates/migration-job.yaml > migration.yaml
```

The migration template has a ServiceAccount, CSI SecretProviderClass (AWS only)
and finite Job. The deployer applies the two named support resources, creates a
unique Job, waits for success and saves logs, then invokes Helm with
`migration.enabled=false`. The Job reads only `MIGRATOR_DATABASE_*` credentials
from a separate Secret. A failed or timed out migration prevents the application
upgrade. No Helm hook silently deletes migration evidence.

API has two replicas, a PDB, strict zone/hostname spread in AWS and one rollout
surge. `/livez` excludes dependencies; `/readyz` checks the DB; ALB exposes only
8080 and terminates HTTPS. Management 9091 has no Service/Ingress and only
Prometheus can reach it through NetworkPolicy. Worker readiness checks bounded
dependencies; independent process/loop health is used for liveness. The consumer
controls pause/reconnect/drain—Kubernetes readiness does not cancel MQ delivery.
API stop budget includes preStop, ALB target removal and Spring shutdown; worker
150-second grace covers the 100-second drain with margin.

ML starts disabled. Enabling it requires the audited model version, immutable ML
image, encoder token in API and encoder Secrets, an ML node group/toleration, and
a larger database/node budget. Every Pod has its own 5Gi model cache; no shared
cross-AZ ReadWriteOnce volume is used. Startup readiness permits a 20-minute cold
load budget, which is an initial configuration rather than a measured duration.

Optional telemetry uses one Prometheus, one trace collector and one namespace
kube-state-metrics instance. Configure a verified HTTPS OTLP endpoint and reviewed
vendor image digests. Queue collector exports real MQ counts with Ready Pod
denominators; worker replicas remain fixed. Enabling these requires platform-owned
read-only namespace RBAC and the corresponding `rbacProvisioned=true` flag.
The app release identity cannot create Roles or RoleBindings. Refer to
[`ops/kubernetes/README.md`](../../ops/kubernetes/README.md) and the connection
budget before changing replica limits.

Validation:

```sh
python -m pip install PyYAML==6.0.3 jsonschema==4.25.1
python -m unittest discover -s deploy/helm/tests -v
python deploy/helm/tests/render_fixtures.py --output-dir work/helm-render \
  --kubeconform /path/to/kubeconform
python -m unittest discover -s ops/kubernetes/queue-collector/tests -v
```

Tests render synthetic dev/prod/local/full values and reject missing Secrets,
mutable images, wrong account/region, missing guards, inadequate budgets and
invalid production/ML settings. Kubernetes built-in schemas are checked at 1.34
and 1.36; the ASCP custom resource is explicitly excluded from built-in schema
lookup and its provider/objects are checked by chart tests. Packaged telemetry
files must equal `ops/kubernetes` sources. These checks do not establish a running
EKS deployment; retained cloud acceptance evidence is required.
