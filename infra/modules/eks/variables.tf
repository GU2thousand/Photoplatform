variable "environment" {
  type = string
  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "Use an independent dev or prod EKS state."
  }
}
variable "region" { type = string }
variable "disposable_environment" {
  description = "Only a dedicated disposable dev deployment may opt in to destructive fault acceptance."
  type        = bool
  default     = false
}
variable "expected_account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.expected_account_id))
    error_message = "Pin the intended 12 digit AWS account."
  }
}
variable "vpc_id" { type = string }
variable "private_subnet_ids" {
  type = set(string)
  validation {
    condition     = length(var.private_subnet_ids) >= 2
    error_message = "Private nodes and cluster ENIs require at least two subnets in different AZs."
  }
}
variable "public_subnet_ids" {
  description = "Existing dedicated ALB-capable public subnets; passed explicitly to Ingress rather than retagged by another state."
  type        = set(string)
  validation {
    condition     = length(var.public_subnet_ids) >= 2
    error_message = "Supply at least two existing ALB public subnets."
  }
}
variable "runner_security_group_ids" {
  description = "Security groups on VPC connected deployment/bootstrap runners, owned by their existing network state."
  type        = set(string)
  validation {
    condition     = length(var.runner_security_group_ids) > 0
    error_message = "Declare a reachable VPC deployment runner before creating a private cluster."
  }
}
variable "dependency_security_group_ids" {
  description = "Existing database and broker security groups. This state owns only its added ingress rules."
  type        = object({ rds = string, mq = string })
}
variable "kubernetes_version" {
  type    = string
  default = "1.36"
  validation {
    condition     = var.kubernetes_version == "1.36"
    error_message = "The reviewed baseline is EKS 1.36. Upgrade this contract and platform pins together."
  }
}
variable "addon_versions" {
  description = "Region-confirmed immutable EKS add-on versions from describe-addon-versions, never most_recent."
  type        = object({ vpc_cni = string, coredns = string, kube_proxy = string, pod_identity_agent = string })
  validation {
    condition     = alltrue([for version in values(var.addon_versions) : can(regex("^v[0-9]+\\.[0-9]+\\.[0-9]+-eksbuild\\.[0-9]+$", version))])
    error_message = "Pin each EKS add-on to a complete vX.Y.Z-eksbuild.N version verified for this region and Kubernetes version."
  }
}
variable "network_policy_enforcing_mode" {
  description = "Initial bootstrap uses standard until kube-system policies exist; switch to strict before any app release."
  type        = string
  default     = "standard"
  validation {
    condition     = contains(["standard", "strict"], var.network_policy_enforcing_mode)
    error_message = "Only documented VPC CNI policy modes are supported. Application release requires strict."
  }
}
variable "node_ami_release_version" {
  description = "Exact EKS AL2023 x86_64 standard release version, checked against the regional SSM recommendation."
  type        = string
  validation {
    condition     = can(regex("^1\\.36\\.[0-9]+-[0-9]{8}$", var.node_ami_release_version))
    error_message = "Pin a 1.36 AL2023 AMI release such as the exact regional SSM release_version; no latest lookup during apply."
  }
}
variable "node_groups" {
  type = map(object({
    instance_types = list(string)
    min_size       = number
    desired_size   = number
    max_size       = number
    workload       = string
  }))
  default = {
    application = { instance_types = ["m7i.large"], min_size = 2, desired_size = 2, max_size = 4, workload = "application" }
    ml          = { instance_types = ["m7i.xlarge"], min_size = 0, desired_size = 0, max_size = 4, workload = "ml" }
  }
  validation {
    condition = contains(keys(var.node_groups), "application") && alltrue([for group in values(var.node_groups) :
      group.min_size >= 0 && group.max_size >= group.desired_size && group.desired_size >= group.min_size &&
      length(group.instance_types) > 0 && contains(["application", "ml"], group.workload)
    ]) && try(var.node_groups.application.min_size >= 2 && var.node_groups.application.workload == "application", false)
    error_message = "Keep at least two application nodes and valid bounded separate application/ML node pools."
  }
}
variable "admin_principal_arns" {
  description = "Named platform/break-glass IAM roles; never the GitHub app release role."
  type        = set(string)
  validation {
    condition     = length(var.admin_principal_arns) > 0 && alltrue([for arn in var.admin_principal_arns : can(regex("^arn:[^:]+:iam::[0-9]{12}:role/", arn))])
    error_message = "Supply at least one existing platform administrator role ARN."
  }
}
variable "github_repository" {
  type    = string
  default = "GU2thousand/Photoplatform"
}
variable "github_oidc_provider_arn" {
  description = "Existing account OIDC provider, managed by the original platform state. This module never creates a duplicate."
  type        = string
  validation {
    condition     = can(regex("^arn:[^:]+:iam::[0-9]{12}:oidc-provider/token\\.actions\\.githubusercontent\\.com$", var.github_oidc_provider_arn))
    error_message = "Use the exact existing GitHub OIDC provider ARN."
  }
}
variable "ecr_repository_arns" {
  description = "Existing immutable API/worker/encoder/collector repositories; ECR ownership stays in the original ECS state."
  type        = set(string)
  validation {
    condition     = length(var.ecr_repository_arns) >= 2 && alltrue([for arn in var.ecr_repository_arns : can(regex("^arn:[^:]+:ecr:[^:]+:[0-9]{12}:repository/[^*?]+$", arn))])
    error_message = "Explicitly reference existing ECR repositories."
  }
}
variable "media_bucket_arn" {
  type = string
  validation {
    condition     = can(regex("^arn:[^:]+:s3:::[^/*?]+$", var.media_bucket_arn))
    error_message = "Use a media bucket ARN, never a wildcard."
  }
}
variable "storage_prefix" {
  description = "Namespace within the shared media bucket; staging/ and media/ are appended by IAM policies."
  type        = string
  validation {
    condition     = length(trim(var.storage_prefix, "/")) > 0 && !can(regex("[*?]", var.storage_prefix))
    error_message = "EKS must have a nonempty exact environment storage prefix without wildcards."
  }
}
variable "media_kms_key_arn" {
  description = "Existing S3 SSE-KMS key if used; null for the existing S3 SSE-S3 setup."
  type        = string
  default     = null
  validation {
    condition     = var.media_kms_key_arn == null ? true : can(regex("^arn:[^:]+:kms:[^:]+:[0-9]{12}:key/[0-9a-f-]+$", var.media_kms_key_arn))
    error_message = "Media KMS permissions require one exact existing key ARN."
  }
}
variable "workload_secret_arns" {
  description = "Exact Secrets Manager ARN allowlist for each workload; never secret values or database administrator credentials."
  type = object({
    api     = set(string), media_worker = set(string), embedding_worker = set(string),
    encoder = set(string), migrator = set(string), queue_collector = set(string)
  })
  validation {
    condition = alltrue([for arn in flatten([for arns in values(var.workload_secret_arns) : tolist(arns)]) :
      can(regex("^arn:[^:]+:secretsmanager:[^:]+:[0-9]{12}:secret:[^*?]+$", arn))
    ]) && length(var.workload_secret_arns.api) > 0 && length(var.workload_secret_arns.media_worker) > 0 && length(var.workload_secret_arns.migrator) > 0
    error_message = "Supply exact full Secret ARNs. API, media worker, and the separate DDL migrator require independent allowlists."
  }
}
variable "secret_kms_key_arns" {
  description = "Per workload customer-managed Secrets Manager KMS keys; only decrypt via Secrets Manager and permitted Secret encryption contexts."
  type        = map(set(string))
  default     = {}
  validation {
    condition     = alltrue([for key in keys(var.secret_kms_key_arns) : contains(["api", "media_worker", "embedding_worker", "encoder", "migrator", "queue_collector"], key)]) && alltrue([for arn in flatten([for arns in values(var.secret_kms_key_arns) : tolist(arns)]) : can(regex("^arn:[^:]+:kms:[^:]+:[0-9]{12}:key/[0-9a-f-]+$", arn))])
    error_message = "KMS permissions must name a defined workload and exact key ARNs without wildcards."
  }
}
variable "forbidden_secret_arns" {
  description = "RDS master/MQ bootstrap/admin Secret ARNs which every workload including migrator must be denied."
  type        = set(string)
  validation {
    condition     = length(var.forbidden_secret_arns) > 0
    error_message = "Inventory and explicitly block bootstrap administrator secrets."
  }
}
variable "frontend_bucket_arn" {
  type = string
  validation {
    condition     = can(regex("^arn:[^:]+:s3:::[^/*?]+$", var.frontend_bucket_arn))
    error_message = "Frontend publishing requires an exact bucket ARN."
  }
}
variable "frontend_distribution_arn" {
  type = string
  validation {
    condition     = can(regex("^arn:[^:]+:cloudfront::[0-9]{12}:distribution/[A-Z0-9]+$", var.frontend_distribution_arn))
    error_message = "Frontend invalidation requires an exact distribution ARN."
  }
}
variable "log_retention_days" {
  type    = number
  default = 30
}
