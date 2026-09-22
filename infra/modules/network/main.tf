data "aws_partition" "current" {}
data "aws_caller_identity" "current" {}

locals {
  azs = { for index, az in var.availability_zones : tostring(index) => az }
  nat_azs = var.single_nat_gateway ? {
    "0" = var.availability_zones[0]
  } : local.azs
  app_security_groups = toset(["api", "worker", "encoder", "collector"])
  # Each entry creates BOTH a source egress and destination ingress rule.
  service_connections = {
    alb_api         = { source = "alb", destination = "api", port = 8080 }
    api_encoder     = { source = "api", destination = "encoder", port = 8090 }
    api_postgres    = { source = "api", destination = "rds", port = 5432 }
    worker_postgres = { source = "worker", destination = "rds", port = 5432 }
    api_amqps       = { source = "api", destination = "mq", port = 5671 }
    worker_amqps    = { source = "worker", destination = "mq", port = 5671 }
  }
}

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = var.name }
}

resource "aws_default_security_group" "this" {
  vpc_id = aws_vpc.this.id
  # Deliberately remove the default allow-all egress and self-ingress rules.
  tags = { Name = "${var.name}-unused-default" }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = var.name }
}

resource "aws_subnet" "public" {
  for_each                = local.azs
  vpc_id                  = aws_vpc.this.id
  availability_zone       = each.value
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, tonumber(each.key))
  map_public_ip_on_launch = false
  tags                    = { Name = "${var.name}-public-${each.value}", Tier = "public" }
}

resource "aws_subnet" "private" {
  for_each                = local.azs
  vpc_id                  = aws_vpc.this.id
  availability_zone       = each.value
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, 10 + tonumber(each.key))
  map_public_ip_on_launch = false
  tags                    = { Name = "${var.name}-app-${each.value}", Tier = "application" }
}

resource "aws_subnet" "data" {
  for_each                = local.azs
  vpc_id                  = aws_vpc.this.id
  availability_zone       = each.value
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, 20 + tonumber(each.key))
  map_public_ip_on_launch = false
  tags                    = { Name = "${var.name}-data-${each.value}", Tier = "data" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${var.name}-public" }
}

resource "aws_route" "internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}

resource "aws_route_table_association" "public" {
  for_each       = local.azs
  subnet_id      = aws_subnet.public[each.key].id
  route_table_id = aws_route_table.public.id
}

resource "aws_eip" "nat" {
  for_each = local.nat_azs
  domain   = "vpc"
  tags     = { Name = "${var.name}-nat-${each.value}" }
}

resource "aws_nat_gateway" "this" {
  for_each      = local.nat_azs
  allocation_id = aws_eip.nat[each.key].id
  subnet_id     = aws_subnet.public[each.key].id
  tags          = { Name = "${var.name}-nat-${each.value}" }
  depends_on    = [aws_internet_gateway.this]
}

resource "aws_route_table" "private" {
  for_each = local.azs
  vpc_id   = aws_vpc.this.id
  tags     = { Name = "${var.name}-app-${each.value}" }
}

resource "aws_route" "nat" {
  for_each               = local.azs
  route_table_id         = aws_route_table.private[each.key].id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.this[var.single_nat_gateway ? "0" : each.key].id
}

resource "aws_route_table_association" "private" {
  for_each       = local.azs
  subnet_id      = aws_subnet.private[each.key].id
  route_table_id = aws_route_table.private[each.key].id
}

resource "aws_route_table" "data" {
  vpc_id = aws_vpc.this.id
  # The data tier intentionally has only the implicit VPC-local route.
  tags = { Name = "${var.name}-isolated-data" }
}

resource "aws_route_table_association" "data" {
  for_each       = local.azs
  subnet_id      = aws_subnet.data[each.key].id
  route_table_id = aws_route_table.data.id
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [for table in aws_route_table.private : table.id]
  tags              = { Name = "${var.name}-s3" }
}

resource "aws_security_group" "service" {
  for_each    = toset(["alb", "api", "worker", "encoder", "collector", "rds", "mq"])
  name_prefix = "${var.name}-${each.key}-"
  description = "${each.key} tier; traffic allowed by explicit service rules"
  vpc_id      = aws_vpc.this.id
  tags        = { Name = "${var.name}-${each.key}" }
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "alb" {
  for_each          = toset(["80", "443"])
  security_group_id = aws_security_group.service["alb"].id
  description       = each.value == "443" ? "Public HTTPS" : "HTTP redirect to HTTPS only"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = tonumber(each.value)
  to_port           = tonumber(each.value)
}

resource "aws_vpc_security_group_ingress_rule" "service" {
  for_each                     = local.service_connections
  security_group_id            = aws_security_group.service[each.value.destination].id
  referenced_security_group_id = aws_security_group.service[each.value.source].id
  description                  = "${each.value.source} to ${each.value.destination}"
  ip_protocol                  = "tcp"
  from_port                    = each.value.port
  to_port                      = each.value.port
}

resource "aws_vpc_security_group_egress_rule" "service" {
  for_each                     = local.service_connections
  security_group_id            = aws_security_group.service[each.value.source].id
  referenced_security_group_id = aws_security_group.service[each.value.destination].id
  description                  = "${each.value.source} to ${each.value.destination}"
  ip_protocol                  = "tcp"
  from_port                    = each.value.port
  to_port                      = each.value.port
}

resource "aws_vpc_security_group_egress_rule" "app_https" {
  for_each          = local.app_security_groups
  security_group_id = aws_security_group.service[each.key].id
  description       = "HTTPS for AWS APIs, registry pulls, model downloads; S3 uses the gateway endpoint"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_ingress_rule" "mq_management" {
  security_group_id            = aws_security_group.service["mq"].id
  referenced_security_group_id = aws_security_group.service["collector"].id
  description                  = "Collector may read RabbitMQ queue depth over TLS"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}
