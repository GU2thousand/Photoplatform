variable "name" { type = string }
variable "region" { type = string }
variable "cluster_name" { type = string }
variable "service_names" { type = map(string) }
variable "alb_arn_suffix" { type = string }
variable "target_group_arn_suffix" { type = string }
variable "rds_identifier" { type = string }
variable "mq_broker_name" { type = string }
variable "alarm_sns_topic_arn" {
  type    = string
  default = null
}
locals {
  worker_dimensions = { ClusterName = var.cluster_name, ServiceName = var.service_names["worker"] }
  alarm_actions     = var.alarm_sns_topic_arn == null ? [] : [var.alarm_sns_topic_arn]
}
resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = var.name
  dashboard_body = jsonencode({ widgets = [
    { type = "metric", width = 12, height = 6, properties = { title = "API rate/errors", region = var.region, period = 60, stat = "Sum", metrics = [
      ["AWS/ApplicationELB", "RequestCount", "LoadBalancer", var.alb_arn_suffix],
      ["AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", "LoadBalancer", var.alb_arn_suffix],
      ["AWS/ApplicationELB", "HTTPCode_ELB_5XX_Count", "LoadBalancer", var.alb_arn_suffix]
    ] } },
    { type = "metric", width = 12, height = 6, properties = { title = "ALB response latency seconds (not individual route latency)", region = var.region, period = 60, metrics = [
      ["AWS/ApplicationELB", "TargetResponseTime", "LoadBalancer", var.alb_arn_suffix, { stat = "p50" }],
      ["AWS/ApplicationELB", "TargetResponseTime", "LoadBalancer", var.alb_arn_suffix, { stat = "p95" }],
      ["AWS/ApplicationELB", "TargetResponseTime", "LoadBalancer", var.alb_arn_suffix, { stat = "p99" }]
    ] } },
    { type = "metric", width = 12, height = 6, properties = { title = "Worker capacity and backlog", region = var.region, period = 60, stat = "Average", metrics = [for metric in ["BacklogPerTask", "QueueDepth", "ActiveTasks", "Ready", "InFlight"] : ["Photoplatform/Workers", metric, "ClusterName", var.cluster_name, "ServiceName", var.service_names["worker"]]] } },
    { type = "metric", width = 12, height = 6, properties = { title = "ECS worker CPU and memory", region = var.region, period = 60, stat = "Average", metrics = [for metric in ["CPUUtilization", "MemoryUtilization"] : ["AWS/ECS", metric, "ClusterName", var.cluster_name, "ServiceName", var.service_names["worker"]]] } },
    { type = "metric", width = 12, height = 6, properties = { title = "RDS connections and read/write latency", region = var.region, period = 60, stat = "Average", metrics = [for metric in ["DatabaseConnections", "ReadLatency", "WriteLatency", "CPUUtilization", "FreeableMemory"] : ["AWS/RDS", metric, "DBInstanceIdentifier", var.rds_identifier]] } },
    { type = "metric", width = 12, height = 6, properties = { title = "Upload starts, completions and abandonment (exported counters)", region = var.region, period = 60, metrics = [[{ expression = "SEARCH('{Photoplatform/Application,ClusterName,ServiceName} ClusterName=\"${var.cluster_name}\" (upload_sessions OR upload_completed OR upload_abandoned)', 'Average', 60)", id = "appmetric", label = "", region = var.region }]] } },
    { type = "metric", width = 12, height = 6, properties = { title = "Worker jobs and failures by outcome", region = var.region, period = 60, metrics = [[{ expression = "SEARCH('{Photoplatform/Application,ClusterName,ServiceName} ClusterName=\"${var.cluster_name}\" (media_jobs OR media_job_failures)', 'Average', 60)", id = "appmetric", label = "", region = var.region }]] } },
    { type = "metric", width = 12, height = 6, properties = { title = "Worker active jobs and broker connectivity", region = var.region, period = 60, metrics = [[{ expression = "SEARCH('{Photoplatform/Application,ClusterName,ServiceName} ClusterName=\"${var.cluster_name}\" (worker_active_jobs OR worker_broker_connected OR worker_broker_reconnects)', 'Average', 60)", id = "appmetric", label = "", region = var.region }]] } },
    { type = "metric", width = 12, height = 6, properties = { title = "Outbox, DB queue backlog, oldest due job age and DLQ jobs", region = var.region, period = 60, metrics = [[{ expression = "SEARCH('{Photoplatform/Application,ClusterName,ServiceName} ClusterName=\"${var.cluster_name}\" (media_outbox_pending OR media_publish_confirmed OR media_publish_failures OR media_dead_letter_jobs OR media_oldest_queued_job_age_seconds OR media_queue_depth)', 'Average', 60)", id = "appmetric", label = "", region = var.region }]] } },
    { type = "metric", width = 12, height = 6, properties = { title = "Database connection/query failures and slow queries", region = var.region, period = 60, metrics = [[{ expression = "SEARCH('{Photoplatform/Application,ClusterName,ServiceName} ClusterName=\"${var.cluster_name}\" (database_connection_failures OR database_query_errors OR database_slow_queries OR worker_database_errors)', 'Average', 60)", id = "appmetric", label = "", region = var.region }]] } },
    { type = "metric", width = 12, height = 6, properties = { title = "Database active/idle/pending connections", region = var.region, period = 60, metrics = [[{ expression = "SEARCH('{Photoplatform/Application,ClusterName,ServiceName} ClusterName=\"${var.cluster_name}\" (hikaricp_connections OR worker_database_connections)', 'Average', 60)", id = "appmetric", label = "", region = var.region }]] } },
    { type = "metric", width = 12, height = 6, properties = { title = "Storage errors and successfully processed bytes", region = var.region, period = 60, metrics = [[{ expression = "SEARCH('{Photoplatform/Application,ClusterName,ServiceName} ClusterName=\"${var.cluster_name}\" (storage_request_errors OR worker_storage_errors OR worker_storage_bytes OR upload_completed_bytes)', 'Average', 60)", id = "appmetric", label = "", region = var.region }]] } },
    { type = "metric", width = 12, height = 6, properties = { title = "Processing / queue wait / SQL timers (sums and counts; p95 in Grafana)", region = var.region, period = 60, metrics = [[{ expression = "SEARCH('{Photoplatform/Application,ClusterName,ServiceName} ClusterName=\"${var.cluster_name}\" (media_processing_duration OR media_queue_wait OR database_query_duration OR worker_database_request_duration)', 'Average', 60)", id = "appmetric", label = "", region = var.region }]] } },
    { type = "text", width = 24, height = 4, properties = { markdown = "Application metrics are exported through ADOT to **Photoplatform/Application** and /photoplatform/${var.name}/metrics; use the existing Prometheus/Grafana dashboard for histogram quantiles, upload lifecycle, jobs, DB query timers and storage operations. CloudWatch infrastructure ReadLatency is storage I/O, **not SQL query latency**. Broker queues: AWS/AmazonMQ, broker **${var.mq_broker_name}**; collector failures emit no zero and trigger the missing-metric alarm. A quiet graph is not evidence of zero failures. Cloud validation and dashboard screenshots are acceptance evidence, not Terraform outputs." } }
  ] })
}
resource "aws_cloudwatch_metric_alarm" "collector_missing" {
  alarm_name          = "${var.name}-worker-metrics-stale"
  alarm_description   = "Missing fresh worker capacity metrics; inspect collector before relying on scaling."
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  metric_name         = "ActiveTasks"
  namespace           = "Photoplatform/Workers"
  period              = 60
  statistic           = "SampleCount"
  threshold           = 1
  treat_missing_data  = "breaching"
  dimensions          = local.worker_dimensions
  alarm_actions       = local.alarm_actions
}
resource "aws_cloudwatch_metric_alarm" "api_unhealthy" {
  alarm_name          = "${var.name}-api-unhealthy"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "UnHealthyHostCount"
  namespace           = "AWS/ApplicationELB"
  period              = 60
  statistic           = "Maximum"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  dimensions          = { LoadBalancer = var.alb_arn_suffix, TargetGroup = var.target_group_arn_suffix }
  alarm_actions       = local.alarm_actions
}
output "dashboard_name" { value = aws_cloudwatch_dashboard.main.dashboard_name }
