terraform {
  required_version = ">= 1.10, < 2.0"
  required_providers { aws = { source = "hashicorp/aws", version = "~> 6.0" } }
  backend "s3" {}
}
provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = "photoplatform", Environment = "dev", ManagedBy = "Terraform", DisposableEnvironment = "true" }
  }
}
module "platform" {
  source                          = "../.."
  environment                     = "dev"
  region                          = var.region
  availability_zones              = var.availability_zones
  bucket_name_prefix              = var.bucket_name_prefix
  vpc_cidr                        = var.vpc_cidr
  api_domain                      = var.api_domain
  api_certificate_arn             = var.api_certificate_arn
  route53_zone_id                 = var.route53_zone_id
  frontend_extra_origins          = var.frontend_extra_origins
  cdn_public_key_pem              = var.cdn_public_key_pem
  storage_prefix                  = var.storage_prefix
  application_secret_arn          = var.application_secret_arn
  mq_secret_arn                   = var.mq_secret_arn
  application_database_secret_arn = var.application_database_secret_arn
  mq_runtime_secret_arn           = var.mq_runtime_secret_arn
  mq_monitoring_secret_arn        = var.mq_monitoring_secret_arn
  create_services                 = var.create_services
  enable_encoder                  = var.enable_encoder
  enable_collector                = var.enable_collector
  image_refs                      = var.image_refs
  api_desired_count               = var.api_desired_count
  worker_min_capacity             = var.worker_min_capacity
  worker_max_capacity             = var.worker_max_capacity
  worker_scaling_mode             = var.worker_scaling_mode
  worker_backlog_target           = var.worker_backlog_target
  worker_cpu_target               = var.worker_cpu_target
  enable_ecs_exec                 = var.enable_ecs_exec
  adot_image                      = var.adot_image
  mq_engine_version               = var.mq_engine_version
  mq_instance_type                = var.mq_instance_type
  rds_engine_version              = var.rds_engine_version
  rds_instance_class              = var.rds_instance_class
  github_repository               = var.github_repository
  github_oidc_provider_arn        = var.github_oidc_provider_arn
  protect_data                    = var.protect_data
  force_destroy_buckets           = var.force_destroy_buckets
  alarm_sns_topic_arn             = var.alarm_sns_topic_arn
}
