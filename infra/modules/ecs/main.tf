variable "name" { type = string }
variable "region" { type = string }
variable "subnet_ids" { type = list(string) }
variable "security_group_ids" { type = map(string) }
variable "target_group_arn" { type = string }
variable "create_services" { type = bool }
variable "enable_encoder" { type = bool }
variable "enable_collector" { type = bool }
variable "image_refs" { type = map(string) }
variable "environments" { type = map(map(string)) }
variable "secrets" { type = map(map(string)) }
variable "media_bucket_arn" { type = string }
variable "storage_prefix" { type = string }
variable "media_distribution_arn" { type = string }
variable "log_retention_days" { type = number }
variable "adot_image" { type = string }
variable "api_desired_count" { type = number }
variable "worker_min_capacity" { type = number }
variable "worker_max_capacity" { type = number }
variable "worker_scaling_mode" { type = string }
variable "worker_backlog_target" { type = number }
variable "worker_cpu_target" { type = number }
variable "enable_ecs_exec" { type = bool }

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
  prefix     = var.storage_prefix == "" ? "" : "${trim(var.storage_prefix, "/")}/"
  workloads = merge({
    api    = { cpu = 1024, memory = 2048, desired = var.api_desired_count, port = 8080 },
    worker = { cpu = 1024, memory = 2048, desired = var.worker_min_capacity, port = 9100 }
    }, var.enable_encoder ? { encoder = { cpu = 2048, memory = 8192, desired = 1, port = 8090 }, embedding-worker = { cpu = 2048, memory = 8192, desired = 1, port = 9100 } } : {},
  var.enable_collector ? { collector = { cpu = 256, memory = 512, desired = 1, port = 0 } } : {})
  service_arns = { for key in keys(local.workloads) : key => "arn:${local.partition}:ecs:${var.region}:${local.account_id}:service/${var.name}/${var.name}-${key}" }
  image_keys   = { for key in keys(local.workloads) : key => key == "embedding-worker" ? "encoder" : key }
  assume_task  = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }] })
  adot_config = { for workload in ["api", "worker", "embedding-worker"] : workload => yamlencode({
    receivers = {
      otlp       = { protocols = { grpc = { endpoint = "0.0.0.0:4317" }, http = { endpoint = "0.0.0.0:4318" } } }
      prometheus = { config = { scrape_configs = [{ job_name = "photoplatform-${workload}", scrape_interval = "30s", metrics_path = workload == "api" ? "/actuator/prometheus" : "/metrics", static_configs = [{ targets = [workload == "api" ? "127.0.0.1:9091" : "127.0.0.1:9100"] }] }] } }
    }
    processors = { batch = { timeout = "30s" }, memory_limiter = { check_interval = "1s", limit_mib = 192 }, resourcedetection = { detectors = ["env", "ecs"], timeout = "5s" }, resource = { attributes = [{ key = "ClusterName", value = var.name, action = "upsert" }, { key = "ServiceName", value = "${var.name}-${workload}", action = "upsert" }] } }
    exporters = {
      awsxray = { region = var.region }
      awsemf = {
        region                           = var.region, namespace = "Photoplatform/Application", log_group_name = "/photoplatform/${var.name}/metrics", log_stream_name = "otel", dimension_rollup_option = "NoDimensionRollup",
        resource_to_telemetry_conversion = { enabled = true }
        metric_declarations              = [{ dimensions = [["ClusterName", "ServiceName"], ["ClusterName", "ServiceName", "type", "outcome"], ["ClusterName", "ServiceName", "operation"]], metric_name_selectors = [".*"] }]
      }
    }
    service = { pipelines = {
      traces  = { receivers = ["otlp"], processors = ["memory_limiter", "resourcedetection", "resource", "batch"], exporters = ["awsxray"] }
      metrics = { receivers = ["otlp", "prometheus"], processors = ["memory_limiter", "resourcedetection", "resource", "batch"], exporters = ["awsemf"] }
    } }
  }) }
}
resource "aws_ecr_repository" "workloads" {
  for_each             = toset(["api", "worker", "encoder", "collector"])
  name                 = "${var.name}/${each.key}"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration { encryption_type = "AES256" }
}
resource "aws_ecr_lifecycle_policy" "workloads" {
  for_each   = aws_ecr_repository.workloads
  repository = each.value.name
  policy = jsonencode({ rules = [{ rulePriority = 1, description = "Expire untagged build layers after 7 days", selection = {
    tagStatus = "untagged", countType = "sinceImagePushed", countUnit = "days", countNumber = 7
  }, action = { type = "expire" } }] })
}
resource "aws_ecs_cluster" "main" {
  name = var.name
  setting {
    name  = "containerInsights"
    value = "enhanced"
  }
}
resource "aws_cloudwatch_log_group" "workloads" {
  for_each          = local.workloads
  name              = "/photoplatform/${var.name}/${each.key}"
  retention_in_days = var.log_retention_days
}
resource "aws_cloudwatch_log_group" "metrics" {
  name              = "/photoplatform/${var.name}/metrics"
  retention_in_days = var.log_retention_days
}
resource "aws_iam_role" "execution" {
  for_each           = local.workloads
  name               = "${var.name}-${each.key}-execution"
  assume_role_policy = local.assume_task
}
resource "aws_iam_role_policy" "execution" {
  for_each = local.workloads
  role     = aws_iam_role.execution[each.key].id
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    { Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*" },
    { Effect = "Allow", Action = ["ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"], Resource = [aws_ecr_repository.workloads[local.image_keys[each.key]].arn] },
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = ["${aws_cloudwatch_log_group.workloads[each.key].arn}:*"] }
    ], length(lookup(var.secrets, each.key, {})) > 0 ? [{ Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = distinct([
      for arn in values(var.secrets[each.key]) : join(":", slice(split(":", arn), 0, 7))
  ]) }] : []) })
}
resource "aws_iam_role" "task" {
  for_each           = local.workloads
  name               = "${var.name}-${each.key}-task"
  assume_role_policy = local.assume_task
}
resource "aws_iam_role_policy" "task" {
  for_each = local.workloads
  role     = aws_iam_role.task[each.key].id
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    { Effect = "Allow", Action = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"], Resource = "*" },
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"], Resource = ["${aws_cloudwatch_log_group.metrics.arn}:*"] }
    ], [for statement in [
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion"], Resource = ["${var.media_bucket_arn}/${local.prefix}staging/*", "${var.media_bucket_arn}/${local.prefix}media/*"] },
      { Effect = "Allow", Action = ["s3:ListBucket", "s3:ListBucketVersions"], Resource = var.media_bucket_arn, Condition = { StringLike = { "s3:prefix" = ["${local.prefix}staging/*", "${local.prefix}media/*"] } } },
      { Effect = "Allow", Action = ["s3:GetBucketVersioning"], Resource = var.media_bucket_arn }
      ] : statement if contains(["api", "worker", "embedding-worker"], each.key)], [for statement in [
      { Effect = "Allow", Action = ["ecs:DescribeServices"], Resource = local.service_arns["worker"] },
      { Effect = "Allow", Action = ["cloudwatch:PutMetricData"], Resource = "*", Condition = { StringEquals = { "cloudwatch:namespace" = "Photoplatform/Workers" } } }
    ] : statement if each.key == "collector"], each.key == "api" ? [{ Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], Resource = ["${var.media_bucket_arn}/${local.prefix}originals/*", "${var.media_bucket_arn}/${local.prefix}thumbnails/*"] }] : [], var.enable_ecs_exec ? [
    { Effect = "Allow", Action = ["ssmmessages:CreateControlChannel", "ssmmessages:CreateDataChannel", "ssmmessages:OpenControlChannel", "ssmmessages:OpenDataChannel"], Resource = "*" }
  ] : []) })
}
resource "aws_ecs_task_definition" "workloads" {
  for_each                 = local.workloads
  family                   = "${var.name}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(each.value.cpu)
  memory                   = tostring(each.value.memory)
  execution_role_arn       = aws_iam_role.execution[each.key].arn
  task_role_arn            = aws_iam_role.task[each.key].arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  container_definitions = jsonencode(concat([{
    name        = each.key
    image       = lookup(var.image_refs, local.image_keys[each.key], "${aws_ecr_repository.workloads[local.image_keys[each.key]].repository_url}:bootstrap")
    essential   = true
    command     = each.key == "encoder" ? ["uvicorn", "app.encoder:app", "--host", "0.0.0.0", "--port", "8090"] : null
    stopTimeout = 120
    healthCheck = each.key == "encoder" ? {
      command  = ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/health',timeout=5)"]
      interval = 30, timeout = 10, retries = 3, startPeriod = 300
      } : contains(["worker", "embedding-worker"], each.key) ? {
      command  = ["CMD", "python", "-m", "app.health"]
      interval = 30, timeout = 10, retries = 3, startPeriod = 60
      } : each.key == "collector" ? {
      command  = ["CMD", "python", "health.py"]
      interval = 30, timeout = 5, retries = 3, startPeriod = 120
    } : null
    environment      = [for name, value in merge(lookup(var.environments, each.key, {}), { AWS_REGION = var.region, AWS_DEFAULT_REGION = var.region }) : { name = name, value = value }]
    secrets          = [for name, value in lookup(var.secrets, each.key, {}) : { name = name, valueFrom = value }]
    portMappings     = each.value.port > 0 ? [{ containerPort = each.value.port, protocol = "tcp" }] : []
    logConfiguration = { logDriver = "awslogs", options = { "awslogs-group" = aws_cloudwatch_log_group.workloads[each.key].name, "awslogs-region" = var.region, "awslogs-stream-prefix" = each.key } }
    }], [for statement in [{
      name             = "otel", image = var.adot_image, essential = false, memoryReservation = 256,
      environment      = [{ name = "AOT_CONFIG_CONTENT", value = lookup(local.adot_config, each.key, "") }],
      logConfiguration = { logDriver = "awslogs", options = { "awslogs-group" = aws_cloudwatch_log_group.workloads[each.key].name, "awslogs-region" = var.region, "awslogs-stream-prefix" = "otel" } }
  }] : statement if contains(["api", "worker", "embedding-worker"], each.key)]))
}
resource "aws_service_discovery_private_dns_namespace" "main" {
  count       = var.enable_encoder ? 1 : 0
  name        = "${var.name}.internal"
  description = "Private encoder discovery"
  vpc         = data.aws_subnet.private.vpc_id
}
data "aws_subnet" "private" { id = var.subnet_ids[0] }
resource "aws_service_discovery_service" "encoder" {
  count = var.enable_encoder ? 1 : 0
  name  = "encoder"
  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.main[0].id
    dns_records {
      ttl  = 10
      type = "A"
    }
    routing_policy = "MULTIVALUE"
  }
  health_check_custom_config {}
}
resource "aws_ecs_service" "workloads" {
  for_each                           = var.create_services ? local.workloads : {}
  name                               = "${var.name}-${each.key}"
  cluster                            = aws_ecs_cluster.main.id
  task_definition                    = aws_ecs_task_definition.workloads[each.key].arn
  desired_count                      = each.value.desired
  launch_type                        = "FARGATE"
  platform_version                   = "1.4.0"
  enable_execute_command             = var.enable_ecs_exec
  enable_ecs_managed_tags            = true
  propagate_tags                     = "SERVICE"
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  health_check_grace_period_seconds  = each.key == "api" ? 120 : null
  wait_for_steady_state              = true
  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = [var.security_group_ids[each.key == "embedding-worker" ? "worker" : each.key]]
    assign_public_ip = false
  }
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  dynamic "load_balancer" {
    for_each = each.key == "api" ? [1] : []
    content {
      target_group_arn = var.target_group_arn
      container_name   = "api"
      container_port   = 8080
    }
  }
  dynamic "service_registries" {
    for_each = each.key == "encoder" ? [1] : []
    content { registry_arn = aws_service_discovery_service.encoder[0].arn }
  }
  # CI owns revision promotion; autoscaling owns running capacity. Terraform owns
  # the service contract, bootstrap definition and infrastructure.
  lifecycle { ignore_changes = [desired_count, task_definition] }
  depends_on = [aws_iam_role_policy.execution, aws_iam_role_policy.task]
}
resource "aws_appautoscaling_target" "worker" {
  count              = var.create_services ? 1 : 0
  min_capacity       = var.worker_min_capacity
  max_capacity       = var.worker_max_capacity
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.workloads["worker"].name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}
resource "aws_appautoscaling_policy" "worker" {
  count              = var.create_services && var.worker_scaling_mode != "disabled" ? 1 : 0
  name               = "${var.name}-worker-${var.worker_scaling_mode}"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.worker[0].resource_id
  scalable_dimension = aws_appautoscaling_target.worker[0].scalable_dimension
  service_namespace  = aws_appautoscaling_target.worker[0].service_namespace
  target_tracking_scaling_policy_configuration {
    target_value       = var.worker_scaling_mode == "backlog" ? var.worker_backlog_target : var.worker_cpu_target
    scale_in_cooldown  = 300
    scale_out_cooldown = 60
    dynamic "predefined_metric_specification" {
      for_each = var.worker_scaling_mode == "cpu" ? [1] : []
      content { predefined_metric_type = "ECSServiceAverageCPUUtilization" }
    }
    dynamic "customized_metric_specification" {
      for_each = var.worker_scaling_mode == "backlog" ? [1] : []
      content {
        namespace   = "Photoplatform/Workers"
        metric_name = "BacklogPerTask"
        statistic   = "Average"
        unit        = "Count"
        dimensions {
          name  = "ClusterName"
          value = aws_ecs_cluster.main.name
        }
        dimensions {
          name  = "ServiceName"
          value = aws_ecs_service.workloads["worker"].name
        }
      }
    }
  }
}
output "cluster_name" { value = aws_ecs_cluster.main.name }
output "cluster_arn" { value = aws_ecs_cluster.main.arn }
output "service_names" { value = { for key in keys(local.workloads) : key => "${var.name}-${key}" } }
output "service_arns" { value = local.service_arns }
output "task_definition_arns" { value = { for key, task in aws_ecs_task_definition.workloads : key => task.arn } }
output "execution_role_arns" { value = { for key, role in aws_iam_role.execution : key => role.arn } }
output "task_role_arns" { value = { for key, role in aws_iam_role.task : key => role.arn } }
output "ecr_repository_urls" { value = { for key, repository in aws_ecr_repository.workloads : key => repository.repository_url } }
output "ecr_repository_arns" { value = { for key, repository in aws_ecr_repository.workloads : key => repository.arn } }
output "log_group_names" { value = { for key, log in aws_cloudwatch_log_group.workloads : key => log.name } }

resource "aws_ssm_parameter" "task_definition" {
  for_each    = aws_ecs_task_definition.workloads
  name        = "/photoplatform/${var.name}/task-definitions/${each.key}"
  type        = "String"
  value       = each.value.arn
  description = "Terraform-reviewed deployment baseline; CI changes only the application image digest."
}
output "task_definition_parameter_names" { value = { for key, parameter in aws_ssm_parameter.task_definition : key => parameter.name } }
output "task_definition_parameter_arns" { value = { for key, parameter in aws_ssm_parameter.task_definition : key => parameter.arn } }

output "service_count" { value = length(aws_ecs_service.workloads) }
output "private_tasks" { value = alltrue([for service in aws_ecs_service.workloads : !service.network_configuration[0].assign_public_ip]) }
