resource "aws_cloudwatch_log_group" "flow" {
  count             = var.enable_flow_logs ? 1 : 0
  name              = "/photoplatform/${var.name}/vpc-flow"
  retention_in_days = var.flow_log_retention_days
}

resource "aws_iam_role" "flow" {
  count       = var.enable_flow_logs ? 1 : 0
  name_prefix = "${var.name}-flow-"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "vpc-flow-logs.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
        ArnLike = {
          "aws:SourceArn" = "arn:${data.aws_partition.current.partition}:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:vpc-flow-log/*"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "flow" {
  count = var.enable_flow_logs ? 1 : 0
  name  = "write-flow-logs"
  role  = aws_iam_role.flow[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
      Resource = "${aws_cloudwatch_log_group.flow[0].arn}:*"
    }]
  })
}

resource "aws_flow_log" "this" {
  count                    = var.enable_flow_logs ? 1 : 0
  vpc_id                   = aws_vpc.this.id
  traffic_type             = "ALL"
  log_destination_type     = "cloud-watch-logs"
  log_destination          = aws_cloudwatch_log_group.flow[0].arn
  iam_role_arn             = aws_iam_role.flow[0].arn
  max_aggregation_interval = 60
  depends_on               = [aws_iam_role_policy.flow]
  tags                     = { Name = var.name }
}
