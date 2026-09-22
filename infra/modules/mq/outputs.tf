output "endpoint" {
  description = "TLS AMQP endpoint without credentials, including amqps:// and port5671."
  value       = local.endpoint
}

output "host" {
  description = "Broker hostname for Spring AMQP connection configuration."
  value       = regex("^amqps://([^:]+):[0-9]+$", local.endpoint)[0]
}

output "management_url" {
  description = "Private RabbitMQ HTTPS management API base URL."
  value       = trimsuffix(aws_mq_broker.this.instances[0].console_url, "/")
}

output "broker_name" {
  value = aws_mq_broker.this.broker_name
}

output "id" {
  value = aws_mq_broker.this.id
}

output "arn" {
  value = aws_mq_broker.this.arn
}

output "publicly_accessible" { value = aws_mq_broker.this.publicly_accessible }
