output "vpc_id" {
  value = aws_vpc.this.id
}

output "public_subnet_ids" {
  value = [for subnet in aws_subnet.public : subnet.id]
}

output "private_subnet_ids" {
  value = [for subnet in aws_subnet.private : subnet.id]
}

output "data_subnet_ids" {
  value = [for subnet in aws_subnet.data : subnet.id]
}

output "security_group_ids" {
  value = { for name, group in aws_security_group.service : name => group.id }
}

output "flow_log_group_name" {
  value = var.enable_flow_logs ? aws_cloudwatch_log_group.flow[0].name : null
}
