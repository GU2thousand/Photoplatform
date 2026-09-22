variable "name" {
  type        = string
  description = "Broker name and resource prefix."
}

variable "subnet_ids" {
  type        = list(string)
  description = "Private data subnets in distinct AZs; one is used for SINGLE_INSTANCE and three for CLUSTER_MULTI_AZ."
  validation {
    condition     = length(var.subnet_ids) >= 1 && length(distinct(var.subnet_ids)) == length(var.subnet_ids)
    error_message = "Provide at least one subnet without duplicates."
  }
}

variable "security_group_id" {
  type        = string
  description = "Security group permitting AMQPS5671 from API/workers and HTTPS443 from the metrics collector."
}

variable "engine_version" {
  type        = string
  description = "Amazon MQ RabbitMQ major.minor release; verify availability in the deployment region."
  default     = "4.3"
  validation {
    condition     = can(regex("^(3\\.13|4\\.[0-9]+)$", var.engine_version))
    error_message = "Use RabbitMQ3.13 or a supported RabbitMQ4 major.minor release (for example4.3), without a patch number."
  }
}

variable "instance_type" {
  type        = string
  description = "Supported Amazon MQ RabbitMQ instance type. m7g.medium is for dev; use m7g.large or larger for production."
  default     = "mq.m7g.medium"
  validation {
    condition     = can(regex("^mq\\.(m5|m7g)\\.", var.instance_type))
    error_message = "Use a supported mq.m5 or mq.m7g broker. Deprecated mq.t3.micro cannot be newly created."
  }
}

variable "deployment_mode" {
  type        = string
  description = "SINGLE_INSTANCE for dev or CLUSTER_MULTI_AZ for a three-node production broker."
  default     = "SINGLE_INSTANCE"
  validation {
    condition     = contains(["SINGLE_INSTANCE", "CLUSTER_MULTI_AZ"], var.deployment_mode)
    error_message = "RabbitMQ deployment mode must be SINGLE_INSTANCE or CLUSTER_MULTI_AZ."
  }
}

variable "mq_username" {
  type        = string
  description = "Initial broker administrator; provision scoped application users before production traffic."
  default     = "photoplatform"
  validation {
    condition     = can(regex("^[A-Za-z0-9_.@-]{2,100}$", var.mq_username)) && var.mq_username != "guest"
    error_message = "Use a non-guest username with2–100 alphanumeric, underscore, dot, at-sign, or hyphen characters."
  }
}

variable "mq_password" {
  type        = string
  description = "Bootstrap broker password. AWS provider stores this value in Terraform state; encrypt and restrict remote state."
  sensitive   = true
  validation {
    condition     = length(var.mq_password) >= 12 && length(var.mq_password) <= 250 && !can(regex("[,=:]", var.mq_password))
    error_message = "MQ password must contain12–250 characters and must not contain comma, equals sign, or colon."
  }
}

variable "maintenance_day" {
  type        = string
  description = "UTC maintenance weekday."
  default     = "SUNDAY"
  validation {
    condition     = contains(["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"], var.maintenance_day)
    error_message = "Use an uppercase weekday name."
  }
}

variable "maintenance_time" {
  type        = string
  description = "UTC maintenance start in HH:MM format."
  default     = "04:00"
  validation {
    condition     = can(regex("^([01][0-9]|2[0-3]):[0-5][0-9]$", var.maintenance_time))
    error_message = "Use a24-hour HH:MM value."
  }
}
