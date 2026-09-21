variable "name" {
  description = "Environment-scoped RDS identifier and resource name prefix."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*[a-z0-9]$", var.name)) && length(var.name) <= 56 && !strcontains(var.name, "--")
    error_message = "name must be 2-56 lowercase letters, digits or hyphens, start with a letter, and not end with or contain consecutive hyphens."
  }
}

variable "subnet_ids" {
  description = "Private subnet IDs spanning at least two Availability Zones."
  type        = list(string)

  validation {
    condition     = length(distinct(var.subnet_ids)) >= 2
    error_message = "RDS requires at least two different private subnets in different Availability Zones."
  }
}

variable "security_group_id" {
  description = "RDS security group permitting PostgreSQL only from authorized ECS security groups."
  type        = string
}

variable "engine_version" {
  description = "PostgreSQL 16 engine version. A major-only version lets AWS select the current supported minor release."
  type        = string
  default     = "16"

  validation {
    condition     = can(regex("^16(\\.[0-9]+)?$", var.engine_version))
    error_message = "engine_version must be PostgreSQL 16 (16 or 16.x), matching the postgres16 parameter group."
  }
}

variable "instance_class" {
  description = "RDS instance class; verify availability in the selected AWS Region before deployment."
  type        = string
  default     = "db.t4g.micro"
}

variable "allocated_storage" {
  description = "Initial encrypted gp3 storage capacity in GiB."
  type        = number
  default     = 20

  validation {
    condition     = var.allocated_storage >= 20 && floor(var.allocated_storage) == var.allocated_storage
    error_message = "allocated_storage must be a whole number of at least 20 GiB."
  }
}

variable "max_allocated_storage" {
  description = "Storage autoscaling ceiling in GiB; zero disables autoscaling."
  type        = number
  default     = 100

  validation {
    condition     = var.max_allocated_storage >= 0 && floor(var.max_allocated_storage) == var.max_allocated_storage
    error_message = "max_allocated_storage must be a nonnegative whole number."
  }
}

variable "multi_az" {
  description = "Provision a synchronous standby in another Availability Zone."
  type        = bool
  default     = false
}

variable "deletion_protection" {
  description = "Protect the instance from deletion; disable deliberately before destroying a protected environment."
  type        = bool
  default     = false
}

variable "backup_retention_days" {
  description = "Automated backup retention in days; zero disables automated backups."
  type        = number
  default     = 7

  validation {
    condition     = var.backup_retention_days >= 0 && var.backup_retention_days <= 35 && floor(var.backup_retention_days) == var.backup_retention_days
    error_message = "backup_retention_days must be a whole number from 0 through 35."
  }
}

variable "skip_final_snapshot" {
  description = "Allow disposable environments to be destroyed without a final snapshot. Set false for durable environments."
  type        = bool
  default     = true
}

variable "final_snapshot_identifier" {
  description = "Optional final snapshot name; defaults to name-final. Choose a new value before deleting a recreated environment whose old snapshot still exists."
  type        = string
  default     = null
}

variable "performance_insights_enabled" {
  description = "Collect Database Insights Standard per-query and database metrics with seven days of retention."
  type        = bool
  default     = true
}

variable "tags" {
  description = "Additional resource tags."
  type        = map(string)
  default     = {}
}
