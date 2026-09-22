locals {
  name              = "photoplatform-${var.environment}"
  db_secret_arn     = coalesce(var.application_database_secret_arn, module.rds.master_secret_arn)
  mq_runtime_arn    = coalesce(var.mq_runtime_secret_arn, var.mq_secret_arn)
  mq_monitoring_arn = coalesce(var.mq_monitoring_secret_arn, var.mq_secret_arn)
  mq_bootstrap      = jsondecode(data.aws_secretsmanager_secret_version.mq.secret_string)
  frontend_url      = "https://${module.cloudfront.distributions["frontend"].domain_name}"
  database_environment = {
    DATABASE_HOST     = module.rds.address, DATABASE_PORT = tostring(module.rds.port), DATABASE_NAME = module.rds.db_name,
    DATABASE_SSL_MODE = "verify-full", DATABASE_SSL_ROOT_CERT = "/app/certs/global-bundle.pem"
  }
  storage_environment = { STORAGE_PROVIDER = "aws", STORAGE_BUCKET = module.s3.buckets["media"].name, STORAGE_REGION = var.region, STORAGE_PREFIX = var.storage_prefix }
  rabbit_environment  = { RABBITMQ_HOST = module.mq.host, RABBITMQ_PORT = "5671", RABBITMQ_SSL_ENABLED = "true" }
  database_secrets    = { DATABASE_USER = "${local.db_secret_arn}:username::", DATABASE_PASSWORD = "${local.db_secret_arn}:password::" }
  rabbit_secrets      = { RABBITMQ_USER = "${local.mq_runtime_arn}:username::", RABBITMQ_PASSWORD = "${local.mq_runtime_arn}:password::" }
  worker_environment = merge(local.database_environment, local.storage_environment, local.rabbit_environment, {
    WORKER_QUEUES               = "media.process,media.delete", SEMANTIC_SEARCH_ENABLED = tostring(var.enable_encoder),
    CLIP_MODEL_VERSION          = "clip-vit-b32-openai-v1", OMP_NUM_THREADS = "1", OPENBLAS_NUM_THREADS = "1",
    OTEL_EXPORTER_OTLP_ENDPOINT = "http://127.0.0.1:4318", OTEL_SERVICE_NAME = "photoplatform-worker"
  })
}

data "aws_secretsmanager_secret_version" "mq" { secret_id = var.mq_secret_arn }
resource "terraform_data" "deployment_requirements" {
  lifecycle {
    precondition {
      condition     = !var.create_services || alltrue([for key in concat(["api", "worker"], var.enable_encoder ? ["encoder"] : [], var.enable_collector ? ["collector"] : []) : can(regex("@sha256:[0-9a-f]{64}$", lookup(var.image_refs, key, "")))])
      error_message = "Services require an existing immutable ECR digest reference for every enabled image; bootstrap ECR first."
    }
    precondition {
      condition     = var.worker_scaling_mode != "backlog" || var.enable_collector
      error_message = "Backlog scaling requires the independent fresh-metric collector."
    }
    precondition {
      condition     = var.environment != "prod" || !var.create_services || (var.application_database_secret_arn != null && var.mq_runtime_secret_arn != null && (!var.enable_collector || var.mq_monitoring_secret_arn != null))
      error_message = "Production services require pre-created restricted database, broker runtime and monitoring user secrets. Run the documented bootstrap migrations first."
    }
    precondition {
      condition     = var.worker_min_capacity >= 1 && var.worker_max_capacity >= var.worker_min_capacity && var.worker_max_capacity <= 8 && var.api_desired_count >= 2
      error_message = "Keep at least two API tasks and 1–8 workers; the collector does not implement scale-from-zero."
    }
  }
}
module "network" {
  source             = "./modules/network"
  name               = local.name
  region             = var.region
  vpc_cidr           = var.vpc_cidr
  availability_zones = var.availability_zones
  single_nat_gateway = var.environment == "dev"
}
module "s3" {
  source           = "./modules/s3"
  name             = var.bucket_name_prefix
  frontend_origins = concat([local.frontend_url], var.frontend_extra_origins)
  storage_prefix   = var.storage_prefix
  force_destroy    = var.force_destroy_buckets
}
module "cloudfront" {
  source         = "./modules/cloudfront"
  name           = local.name
  buckets        = module.s3.buckets
  public_key_pem = var.cdn_public_key_pem
  storage_prefix = var.storage_prefix
}
module "rds" {
  source                = "./modules/rds"
  name                  = local.name
  subnet_ids            = module.network.data_subnet_ids
  security_group_id     = module.network.security_group_ids["rds"]
  engine_version        = var.rds_engine_version
  instance_class        = var.rds_instance_class
  multi_az              = var.environment == "prod"
  deletion_protection   = var.protect_data
  backup_retention_days = var.environment == "prod" ? 30 : 7
  skip_final_snapshot   = var.environment == "dev"
}
module "mq" {
  source            = "./modules/mq"
  name              = local.name
  subnet_ids        = module.network.data_subnet_ids
  security_group_id = module.network.security_group_ids["mq"]
  engine_version    = var.mq_engine_version
  instance_type     = var.mq_instance_type
  deployment_mode   = var.environment == "prod" ? "CLUSTER_MULTI_AZ" : "SINGLE_INSTANCE"
  mq_username       = local.mq_bootstrap.username
  mq_password       = local.mq_bootstrap.password
}
module "alb" {
  source              = "./modules/alb"
  name                = local.name
  vpc_id              = module.network.vpc_id
  subnet_ids          = module.network.public_subnet_ids
  security_group_id   = module.network.security_group_ids["alb"]
  certificate_arn     = var.api_certificate_arn
  deletion_protection = var.protect_data
}
resource "aws_route53_record" "api" {
  count   = var.route53_zone_id == null ? 0 : 1
  zone_id = var.route53_zone_id
  name    = var.api_domain
  type    = "A"
  alias {
    name                   = module.alb.dns_name
    zone_id                = module.alb.zone_id
    evaluate_target_health = true
  }
}
module "ecs" {
  source                 = "./modules/ecs"
  name                   = local.name
  region                 = var.region
  subnet_ids             = module.network.private_subnet_ids
  security_group_ids     = module.network.security_group_ids
  target_group_arn       = module.alb.target_group_arn
  create_services        = var.create_services
  enable_encoder         = var.enable_encoder
  enable_collector       = var.enable_collector
  image_refs             = var.image_refs
  media_bucket_arn       = module.s3.buckets["media"].arn
  storage_prefix         = var.storage_prefix
  media_distribution_arn = module.cloudfront.distributions["media"].arn
  log_retention_days     = var.environment == "prod" ? 90 : 14
  adot_image             = var.adot_image
  api_desired_count      = var.api_desired_count
  worker_min_capacity    = var.worker_min_capacity
  worker_max_capacity    = var.worker_max_capacity
  worker_scaling_mode    = var.worker_scaling_mode
  worker_backlog_target  = var.worker_backlog_target
  worker_cpu_target      = var.worker_cpu_target
  enable_ecs_exec        = var.enable_ecs_exec
  environments = {
    api = merge(local.storage_environment, local.rabbit_environment, {
      SPRING_PROFILES_ACTIVE      = "aws", PORT = "8080", MANAGEMENT_SERVER_PORT = "9091", MANAGEMENT_SERVER_ADDRESS = "127.0.0.1",
      SPRING_DATASOURCE_URL       = "jdbc:postgresql://${module.rds.address}:${module.rds.port}/${module.rds.db_name}?sslmode=verify-full&sslrootcert=/app/certs/global-bundle.pem",
      DATABASE_SSL_MODE           = "verify-full", DATABASE_SSL_ROOT_CERT = "/app/certs/global-bundle.pem", SPRING_FLYWAY_ENABLED = tostring(var.application_database_secret_arn == null),
      APP_SEED_ENABLED            = "false", APP_CORS_ALLOWED_ORIGINS = join(",", concat([local.frontend_url], var.frontend_extra_origins)),
      MEDIA_PIPELINE_ENABLED      = "true", MEDIA_URL_PROVIDER = "cloudfront", MEDIA_URL_TTL_SECONDS = "60",
      CDN_DOMAIN                  = module.cloudfront.distributions["media"].domain_name, CDN_KEY_PAIR_ID = module.cloudfront.key_pair_id,
      SEMANTIC_SEARCH_ENABLED     = tostring(var.enable_encoder), ENCODER_URL = "http://encoder.${local.name}.internal:8090",
      CLIP_MODEL_VERSION          = "clip-vit-b32-openai-v1", JAVA_TOOL_OPTIONS = "-Xmx1024m -javaagent:/app/opentelemetry-javaagent.jar", OTEL_SDK_DISABLED = "false",
      OTEL_EXPORTER_OTLP_ENDPOINT = "http://127.0.0.1:4317", OTEL_EXPORTER_OTLP_PROTOCOL = "grpc", OTEL_SERVICE_NAME = "photoplatform-api"
    })
    worker           = local.worker_environment
    embedding-worker = merge(local.worker_environment, { WORKER_QUEUES = "media.embed", TORCH_THREADS = "2", OTEL_SERVICE_NAME = "photoplatform-embedding-worker" })
    encoder          = { CLIP_MODEL_VERSION = "clip-vit-b32-openai-v1", TORCH_THREADS = "2" }
    collector        = { ECS_CLUSTER = local.name, ECS_SERVICE = "${local.name}-worker", RABBITMQ_HOST = module.mq.host, RABBITMQ_MANAGEMENT_URL = module.mq.management_url, WORKER_QUEUES = "media.process,media.delete", METRIC_INTERVAL_SECONDS = "60" }
  }
  secrets = {
    api = merge(local.rabbit_secrets, {
      SPRING_DATASOURCE_USERNAME = "${local.db_secret_arn}:username::", SPRING_DATASOURCE_PASSWORD = "${local.db_secret_arn}:password::",
      APP_JWT_SECRET             = "${var.application_secret_arn}:jwt_secret::", CDN_PRIVATE_KEY_PEM = "${var.application_secret_arn}:cdn_private_key_pem::", ENCODER_TOKEN = "${var.application_secret_arn}:encoder_token::"
    })
    worker           = merge(local.database_secrets, local.rabbit_secrets)
    embedding-worker = merge(local.database_secrets, local.rabbit_secrets)
    encoder          = { ENCODER_TOKEN = "${var.application_secret_arn}:encoder_token::" }
    collector        = { RABBITMQ_USER = "${local.mq_monitoring_arn}:username::", RABBITMQ_PASSWORD = "${local.mq_monitoring_arn}:password::" }
  }
  depends_on = [terraform_data.deployment_requirements, module.alb]
}
