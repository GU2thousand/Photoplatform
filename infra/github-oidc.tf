data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
resource "aws_iam_openid_connect_provider" "github" {
  count          = var.github_oidc_provider_arn == null ? 1 : 0
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}
resource "aws_iam_role" "github_deploy" {
  name = "${local.name}-github-deploy"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
    Principal = { Federated = var.github_oidc_provider_arn != null ? var.github_oidc_provider_arn : aws_iam_openid_connect_provider.github[0].arn },
    Condition = { StringEquals = {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
      "token.actions.githubusercontent.com:sub" = "repo:${var.github_repository}:environment:${var.environment}"
    } }
  }] })
}
resource "aws_iam_role_policy" "github_deploy" {
  role = aws_iam_role.github_deploy.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["ssm:GetParameters", "ssm:GetParameter"], Resource = values(module.ecs.task_definition_parameter_arns) },
    { Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*" },
    { Effect = "Allow", Action = ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:DescribeImages"], Resource = values(module.ecs.ecr_repository_arns) },
    { Effect = "Allow", Action = ["ecs:DescribeServices", "ecs:UpdateService"], Resource = values(module.ecs.service_arns) },
    { Effect = "Allow", Action = ["ecs:DescribeTaskDefinition", "ecs:RegisterTaskDefinition"], Resource = "*" },
    { Effect = "Allow", Action = ["ecs:TagResource"], Resource = "arn:${data.aws_partition.current.partition}:ecs:${var.region}:${data.aws_caller_identity.current.account_id}:task-definition/${local.name}-*:*" },
    { Effect = "Allow", Action = ["iam:PassRole"], Resource = concat(values(module.ecs.task_role_arns), values(module.ecs.execution_role_arns)), Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } } },
    { Effect = "Allow", Action = ["s3:ListBucket"], Resource = module.s3.buckets["frontend"].arn },
    { Effect = "Allow", Action = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"], Resource = "${module.s3.buckets["frontend"].arn}/*" },
    { Effect = "Allow", Action = ["cloudfront:CreateInvalidation", "cloudfront:GetInvalidation"], Resource = module.cloudfront.distributions["frontend"].arn }
  ] })
}
