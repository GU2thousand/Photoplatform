locals {
  tags = merge(var.tags, { Name = var.name, Component = "database" })
}

resource "aws_db_subnet_group" "this" {
  name        = var.name
  description = "Private subnets for ${var.name} PostgreSQL"
  subnet_ids  = var.subnet_ids
  tags        = local.tags
}

resource "aws_db_parameter_group" "this" {
  name_prefix = "${var.name}-"
  family      = "postgres16"
  description = "TLS and slow query logging for ${var.name}"

  parameter {
    name         = "rds.force_ssl"
    value        = "1"
    apply_method = "pending-reboot"
  }

  parameter {
    name         = "log_min_duration_statement"
    value        = "500"
    apply_method = "immediate"
  }

  tags = local.tags

  lifecycle {
    create_before_destroy = true
  }
}

# Create the groups first so RDS does not create unmanaged groups with unlimited
# retention as soon as log export is enabled.
resource "aws_cloudwatch_log_group" "database" {
  for_each          = toset(["postgresql", "upgrade"])
  name              = "/aws/rds/instance/${var.name}/${each.key}"
  retention_in_days = 30
  tags              = local.tags
}

resource "aws_db_instance" "this" {
  identifier     = var.name
  engine         = "postgres"
  engine_version = var.engine_version
  instance_class = var.instance_class

  db_name                         = "photoplatform"
  username                        = "photoplatform_admin"
  manage_master_user_password     = true
  port                            = 5432
  db_subnet_group_name            = aws_db_subnet_group.this.name
  vpc_security_group_ids          = [var.security_group_id]
  parameter_group_name            = aws_db_parameter_group.this.name
  publicly_accessible             = false
  storage_type                    = "gp3"
  storage_encrypted               = true
  allocated_storage               = var.allocated_storage
  max_allocated_storage           = var.max_allocated_storage
  multi_az                        = var.multi_az
  deletion_protection             = var.deletion_protection
  backup_retention_period         = var.backup_retention_days
  auto_minor_version_upgrade      = true
  copy_tags_to_snapshot           = true
  skip_final_snapshot             = var.skip_final_snapshot
  final_snapshot_identifier       = var.skip_final_snapshot ? null : coalesce(var.final_snapshot_identifier, "${var.name}-final")
  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]

  # The API retains the Performance Insights field names; Database Insights
  # Standard is the current console/service mode for these seven-day metrics.
  database_insights_mode                = "standard"
  performance_insights_enabled          = var.performance_insights_enabled
  performance_insights_retention_period = var.performance_insights_enabled ? 7 : null

  tags       = local.tags
  depends_on = [aws_cloudwatch_log_group.database]

  lifecycle {
    precondition {
      condition     = var.max_allocated_storage == 0 || var.max_allocated_storage >= ceil(var.allocated_storage * 1.1)
      error_message = "Storage autoscaling must be disabled with zero or have a ceiling at least 10% above allocated_storage."
    }
  }
}
