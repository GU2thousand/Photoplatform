variable "name" {
  description = "Resource name prefix, unique within this AWS account and region."
  type        = string
}

variable "region" {
  description = "AWS region used for the S3 gateway endpoint."
  type        = string
}

variable "vpc_cidr" {
  description = "IPv4 VPC CIDR. The module reserves public, application, and data subnet ranges."
  type        = string
  default     = "10.40.0.0/16"
  validation {
    condition     = can(cidrsubnet(var.vpc_cidr, 8, 22)) && can(regex("/1[6-9]$|/20$", var.vpc_cidr))
    error_message = "Use a valid IPv4 CIDR with a prefix between /16 and /20."
  }
}

variable "availability_zones" {
  description = "Three distinct availability zones in this region, in stable order."
  type        = list(string)
  validation {
    condition     = length(var.availability_zones) == 3 && length(distinct(var.availability_zones)) == 3
    error_message = "Exactly three distinct availability zones are required for the data tier and MQ cluster."
  }
}

variable "single_nat_gateway" {
  description = "Use one NAT gateway for lower dev cost. Set false for an independent NAT gateway in each AZ."
  type        = bool
  default     = true
}

variable "enable_flow_logs" {
  description = "Publish accepted and rejected VPC flows to CloudWatch Logs."
  type        = bool
  default     = true
}

variable "flow_log_retention_days" {
  type        = number
  description = "CloudWatch retention for VPC flow logs."
  default     = 30
}
