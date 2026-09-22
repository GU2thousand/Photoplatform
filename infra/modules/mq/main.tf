resource "aws_mq_broker" "this" {
  broker_name                = var.name
  engine_type                = "RabbitMQ"
  engine_version             = var.engine_version
  host_instance_type         = var.instance_type
  deployment_mode            = var.deployment_mode
  subnet_ids                 = var.deployment_mode == "SINGLE_INSTANCE" ? slice(var.subnet_ids, 0, 1) : var.subnet_ids
  security_groups            = [var.security_group_id]
  publicly_accessible        = false
  auto_minor_version_upgrade = true
  apply_immediately          = false
  authentication_strategy    = "simple"

  encryption_options {
    use_aws_owned_key = true
  }

  logs {
    general = true
  }

  maintenance_window_start_time {
    day_of_week = var.maintenance_day
    time_of_day = var.maintenance_time
    time_zone   = "UTC"
  }

  user {
    username = var.mq_username
    password = var.mq_password
  }

  lifecycle {
    precondition {
      condition     = var.deployment_mode != "CLUSTER_MULTI_AZ" || length(var.subnet_ids) == 3
      error_message = "CLUSTER_MULTI_AZ requires exactly three subnets in distinct availability zones."
    }
    precondition {
      condition     = !startswith(var.engine_version, "4.") || startswith(var.instance_type, "mq.m7g.")
      error_message = "RabbitMQ4 on Amazon MQ requires the mq.m7g instance family."
    }
    precondition {
      condition     = var.deployment_mode != "CLUSTER_MULTI_AZ" || var.instance_type != "mq.m7g.medium"
      error_message = "Use mq.m7g.large or larger for a RabbitMQ cluster."
    }
  }

  tags = { Name = var.name }
}

locals {
  endpoint = aws_mq_broker.this.instances[0].endpoints[0]
}
