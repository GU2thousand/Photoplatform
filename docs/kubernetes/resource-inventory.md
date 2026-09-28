# Resource inventory and AWS evidence

Status: account-side inventory is not available in this implementation. The repository's Terraform defines possible resources; that does not establish that they exist. Fill one record for dev and one for prod from an authenticated AWS session, and retain it without secret payloads in each acceptance run. Do not copy example IDs from tfvars fixtures into a deployment.

| Required field | Current evidence | Approval/acceptance check |
| --- | --- | --- |
| Account, region, platform role, budget owner | Not provided | STS identity, allowed-account guard, signed budget sheet |
| Existing backend bucket/key/state serial/version | Not provided | Existing owner identified and state backup secured |
| VPC/DNS and public/private/data subnet IDs | Not provided | Same VPC, >=2 AZs, route tables and DNS verified |
| Subnet free IPs and node/Pod/surge forecast | Not measured | `AvailableIpAddressCount`, ENI limit and CNI allocation budget |
| NAT or ECR/S3/Secrets/EKS Auth/telemetry endpoints | Not provided | Actual routes, endpoint SGs and dependency TLS test |
| Dedicated private CI/bootstrap runner and SG | Not provided | Private API DNS/TLS reachable from selected runner |
| RDS identifier/hostname/version/pgvector/backups/SG | Not provided | No public DB, restore/PITR evidence, scoped DB users |
| MQ broker ARN/hostname/version/SG and user ACLs | Not provided | AMQPS+443 only private; runtime and monitoring separated |
| Frontend/media bucket and CloudFront IDs/ARNs | Not provided | Block Public Access, encryption/versioning, CORS, exact storage prefix |
| ECR repository ARN/URI/owner/digests | Not provided | Immutable tags, final main provenance and image architecture |
| Existing GitHub OIDC provider and protected environments | Not provided | Exact repository/environment subject; deploy role has no cluster admin |
| Secrets/KMS ARNs and Secret version IDs | Not provided | Runtime/migrator/token/admin boundaries, no values in evidence |
| EKS API domain/ACM and Route53 owner | Not provided | Dedicated EKS hostname and no production ECS DNS change |
| Regional EKS/add-on/AL2023 release versions | Required explicit inputs | Official regional API responses and exact version strings |
| Existing ECS capacity/consumer state and public origin | Not provided | Explicit pre-cutover consumer ownership decision |

The original root now exports `vpc_id`, `public_subnet_ids`, `ecr_repository_arns`, `github_oidc_provider_arn`, `media_bucket_arn`, `frontend_bucket_arn` and `frontend_distribution_arn` alongside existing private-subnet/SG/endpoint outputs. Export only these non-secret output values. Do not grant the app release role general access to the old state or dump `terraform state pull` into GitHub artifacts.

Example inventory record (replace every null with authenticated evidence; a null is blocked, not zero):

```json
{
  "recorded_at": null,
  "repository_sha": null,
  "verify_run_id": null,
  "aws_account_id": null,
  "region": null,
  "shared_state": {"owner": null, "key": null, "serial": null, "version_id": null},
  "eks_state": {"key": null, "serial": null},
  "vpc_id": null,
  "private_subnets": [{"id": null, "az": null, "free_ipv4": null, "routes_verified": null}],
  "runner": {"identity_role_arn": null, "security_group_ids": [], "private_api_tls_verified": null},
  "versions": {"eks": "1.36", "al2023_release": null, "addon_versions": null},
  "shared_resources": [],
  "expected_monthly_cost_usd": null,
  "budget_owner": null,
  "status": "blocked-pending-account-inventory"
}
```

Each shared-resource entry should contain physical ARN/ID, owner state/address, environment, deletion/backup protection, intended EKS use and verification timestamp. Use full Secret ARNs/version IDs only. Authentication values, JWTs, signed URLs, passwords and private keys do not belong in inventory.

Before a plan, identify security-group rule ownership: the current network module uses standalone rules, so the EKS state can own its three new inbound grants without importing the parent groups. If an actual deployed SG uses authoritative inline ingress or is managed by another controller, resolve that conflict in its existing owner before adding EKS rules. A standalone rule and authoritative inline list must not compete.

Live acceptance adds cluster ARN/version/platform version, KMS ARN, node/AMI/add-on records, Pod Identity association IDs, ServiceAccount names, ALB ARN/hostname/target health, actual Pod UIDs/imageIDs, namespace UID, Helm revision, migrated schema/Flyway history, requests/error denominators and the current monthly estimate. Failed and incomplete checks stay in the evidence with their reason. The [acceptance matrix](acceptance.md) determines completion; the existence of an inventory file does not.
