terraform {
  required_version = ">= 1.10, < 2.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "= 6.66.0" }
  }
  backend "s3" {}
}
provider "aws" {
  region              = var.region
  allowed_account_ids = [var.expected_account_id]
  default_tags {
    tags = { Project = "photoplatform", Environment = "prod", ComputeRoute = "eks", DisposableEnvironment = tostring(var.disposable_environment), ManagedBy = "Terraform", ResourceOwner = "eks-compute" }
  }
}
module "eks" {
  disposable_environment        = var.disposable_environment
  network_policy_enforcing_mode = var.network_policy_enforcing_mode
  source                        = "../../modules/eks"
  environment                   = "prod"
  region                        = var.region
  expected_account_id           = var.expected_account_id
  vpc_id                        = var.vpc_id
  private_subnet_ids            = var.private_subnet_ids
  public_subnet_ids             = var.public_subnet_ids
  runner_security_group_ids     = var.runner_security_group_ids
  dependency_security_group_ids = var.dependency_security_group_ids
  kubernetes_version            = var.kubernetes_version
  addon_versions                = var.addon_versions
  node_ami_release_version      = var.node_ami_release_version
  node_groups                   = var.node_groups
  admin_principal_arns          = var.admin_principal_arns
  github_repository             = var.github_repository
  github_oidc_provider_arn      = var.github_oidc_provider_arn
  ecr_repository_arns           = var.ecr_repository_arns
  media_bucket_arn              = var.media_bucket_arn
  storage_prefix                = var.storage_prefix
  media_kms_key_arn             = var.media_kms_key_arn
  workload_secret_arns          = var.workload_secret_arns
  secret_kms_key_arns           = var.secret_kms_key_arns
  forbidden_secret_arns         = var.forbidden_secret_arns
  frontend_bucket_arn           = var.frontend_bucket_arn
  frontend_distribution_arn     = var.frontend_distribution_arn
  log_retention_days            = var.log_retention_days
}
