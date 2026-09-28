# No existing subnet, VPC, ECS ALB or security group is taken into this state.
# Node SGs are associated with VPC CNI pod ENIs. NetworkPolicy narrows per-pod traffic.
resource "aws_security_group" "control_plane" {
  name_prefix = "${local.name}-control-"
  description = "Private EKS API and kubelet access"
  vpc_id      = var.vpc_id
}
resource "aws_security_group" "node" {
  name_prefix = "${local.name}-node-"
  description = "EKS private CPU node and VPC CNI pod ENIs"
  vpc_id      = var.vpc_id
}
resource "aws_security_group" "alb" {
  name_prefix = "${local.name}-alb-"
  description = "Dedicated EKS controller ALB, business HTTPS only"
  vpc_id      = var.vpc_id
}
resource "aws_vpc_security_group_ingress_rule" "api_from_node" {
  security_group_id            = aws_security_group.control_plane.id
  referenced_security_group_id = aws_security_group.node.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}
resource "aws_vpc_security_group_ingress_rule" "api_from_runner" {
  for_each                     = var.runner_security_group_ids
  security_group_id            = aws_security_group.control_plane.id
  referenced_security_group_id = each.value
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}
resource "aws_vpc_security_group_egress_rule" "control_to_node" {
  for_each                     = toset(["443", "9443", "10250"])
  security_group_id            = aws_security_group.control_plane.id
  referenced_security_group_id = aws_security_group.node.id
  ip_protocol                  = "tcp"
  from_port                    = tonumber(each.value)
  to_port                      = tonumber(each.value)
}
resource "aws_vpc_security_group_ingress_rule" "node_from_control" {
  for_each                     = toset(["443", "9443", "10250"])
  security_group_id            = aws_security_group.node.id
  referenced_security_group_id = aws_security_group.control_plane.id
  ip_protocol                  = "tcp"
  from_port                    = tonumber(each.value)
  to_port                      = tonumber(each.value)
}
resource "aws_vpc_security_group_ingress_rule" "node_self" {
  security_group_id            = aws_security_group.node.id
  referenced_security_group_id = aws_security_group.node.id
  ip_protocol                  = "-1"
}
resource "aws_vpc_security_group_egress_rule" "node_self" {
  security_group_id            = aws_security_group.node.id
  referenced_security_group_id = aws_security_group.node.id
  ip_protocol                  = "-1"
}
resource "aws_vpc_security_group_egress_rule" "node_api" {
  security_group_id            = aws_security_group.node.id
  referenced_security_group_id = aws_security_group.control_plane.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}
resource "aws_vpc_security_group_egress_rule" "node_dns" {
  for_each          = toset(["udp", "tcp"])
  security_group_id = aws_security_group.node.id
  cidr_ipv4         = data.aws_vpc.shared.cidr_block
  ip_protocol       = each.value
  from_port         = 53
  to_port           = 53
}
resource "aws_vpc_security_group_egress_rule" "node_https" {
  security_group_id = aws_security_group.node.id
  description       = "HTTPS via existing NAT or VPC endpoints: ECR, S3, Secrets Manager, EKS Auth and telemetry"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}
resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  security_group_id = aws_security_group.alb.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}
resource "aws_vpc_security_group_egress_rule" "alb_business" {
  security_group_id            = aws_security_group.alb.id
  referenced_security_group_id = aws_security_group.node.id
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
}
resource "aws_vpc_security_group_ingress_rule" "node_business" {
  security_group_id            = aws_security_group.node.id
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
}
locals {
  dependency_ports = {
    postgres       = { group = var.dependency_security_group_ids.rds, port = 5432 }
    amqps          = { group = var.dependency_security_group_ids.mq, port = 5671 }
    broker_metrics = { group = var.dependency_security_group_ids.mq, port = 443 }
  }
}
resource "aws_vpc_security_group_ingress_rule" "dependency" {
  for_each                     = local.dependency_ports
  description                  = "${local.name} private ${each.key}; owned by EKS state"
  security_group_id            = each.value.group
  referenced_security_group_id = aws_security_group.node.id
  ip_protocol                  = "tcp"
  from_port                    = each.value.port
  to_port                      = each.value.port
}
resource "aws_vpc_security_group_egress_rule" "dependency" {
  for_each                     = local.dependency_ports
  security_group_id            = aws_security_group.node.id
  referenced_security_group_id = each.value.group
  ip_protocol                  = "tcp"
  from_port                    = each.value.port
  to_port                      = each.value.port
}
