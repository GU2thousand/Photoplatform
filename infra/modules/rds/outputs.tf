output "endpoint" {
  description = "PostgreSQL DNS endpoint including its port."
  value       = aws_db_instance.this.endpoint
}

output "address" {
  description = "PostgreSQL DNS hostname without the port."
  value       = aws_db_instance.this.address
}

output "port" {
  description = "PostgreSQL listener port."
  value       = aws_db_instance.this.port
}

output "db_name" {
  description = "Initial application database name."
  value       = aws_db_instance.this.db_name
}

output "master_username" {
  description = "Initial database administrator username; provision a restricted application role before production use."
  value       = aws_db_instance.this.username
}

output "master_secret_arn" {
  description = "ARN of the password secret managed by RDS. Secret contents are never read into Terraform state by this module."
  value       = aws_db_instance.this.master_user_secret[0].secret_arn
}

output "identifier" {
  description = "RDS instance identifier used for dashboards, alarms and operational commands."
  value       = aws_db_instance.this.identifier
}

output "publicly_accessible" { value = aws_db_instance.this.publicly_accessible }
