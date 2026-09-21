variable "region" {
  type    = string
  default = "us-east-1"
}
variable "environment" {
  type = string
  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "Use dev or prod."
  }
}
variable "availability_zones" { type = list(string) }
variable "bucket_name_prefix" {
  type        = string
  description = "Globally unique lowercase prefix; media/frontend are appended."
}
variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
}
variable "api_domain" { type = string }
variable "api_certificate_arn" {
  type        = string
  description = "Existing validated ACM certificate in this AWS region covering api_domain."
}
variable "route53_zone_id" {
  type    = string
  default = null
}
variable "frontend_extra_origins" {
  type    = list(string)
  default = []
}
variable "cdn_public_key_pem" { type = string }
variable "storage_prefix" {
  type    = string
  default = ""
  validation {
    condition     = var.storage_prefix == "" || can(regex("^[A-Za-z0-9][A-Za-z0-9/_-]*[A-Za-z0-9]$", var.storage_prefix))
    error_message = "Prefix is empty or a relative path without leading/trailing slash."
  }
}
variable "application_secret_arn" {
  type        = string
  description = "Existing Secrets Manager JSON with jwt_secret, cdn_private_key_pem, encoder_token; Terraform does not read contents."
}
variable "mq_secret_arn" {
  type        = string
  description = "Existing JSON secret with username,password. MQ bootstrap password enters sensitive Terraform state; restrict/encrypt state."
}
variable "application_database_secret_arn" {
  type        = string
  default     = null
  description = "Optional existing JSON username/password for a pre-created restricted PostgreSQL application role. Required for prod services."
}
variable "mq_runtime_secret_arn" {
  type        = string
  default     = null
  description = "Optional JSON username/password for a pre-created vhost-scoped broker application user. Required for prod services."
}
variable "mq_monitoring_secret_arn" {
  type        = string
  default     = null
  description = "Optional JSON username/password for a read-only RabbitMQ monitoring user. Required for prod collector."
}
variable "create_services" {
  type    = bool
  default = false
}
variable "enable_encoder" {
  type    = bool
  default = false
}
variable "enable_collector" {
  type    = bool
  default = true
}
variable "image_refs" {
  type        = map(string)
  default     = {}
  description = "Complete ECR digest references keyed api,worker,encoder,collector; required when services enabled."
}
variable "api_desired_count" {
  type    = number
  default = 2
}
variable "worker_min_capacity" {
  type    = number
  default = 1
}
variable "worker_max_capacity" {
  type    = number
  default = 8
}
variable "worker_scaling_mode" {
  type    = string
  default = "backlog"
  validation {
    condition     = contains(["cpu", "backlog", "disabled"], var.worker_scaling_mode)
    error_message = "Use cpu, backlog, or disabled."
  }
}
variable "worker_backlog_target" {
  type    = number
  default = 20
}
variable "worker_cpu_target" {
  type    = number
  default = 60
}
variable "enable_ecs_exec" {
  type    = bool
  default = false
}
variable "adot_image" {
  type    = string
  default = "public.ecr.aws/aws-observability/aws-otel-collector:v0.43.3"
}
variable "mq_engine_version" {
  type    = string
  default = "4.3"
}
variable "mq_instance_type" {
  type    = string
  default = "mq.m7g.medium"
}
variable "rds_engine_version" {
  type    = string
  default = "16"
}
variable "rds_instance_class" {
  type    = string
  default = "db.t4g.micro"
}
variable "github_repository" {
  type    = string
  default = "GU2thousand/Photoplatform"
}
variable "github_oidc_provider_arn" {
  type        = string
  default     = null
  description = "Reuse existing account GitHub OIDC provider; otherwise this stack creates one (only one per account)."
}
variable "protect_data" {
  type        = bool
  default     = true
  description = "ALB/RDS deletion protection; disable deliberately for teardown. Does not empty buckets/ECR."
}
variable "force_destroy_buckets" {
  type    = bool
  default = false
}
variable "alarm_sns_topic_arn" {
  type    = string
  default = null
}
