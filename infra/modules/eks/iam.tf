locals {
  workload_trust_policies = { for key, account in local.service_accounts : key => jsonencode({
    Version = "2012-10-17", Statement = [{
      Effect = "Allow", Principal = { Service = "pods.eks.amazonaws.com" }, Action = ["sts:AssumeRole", "sts:TagSession"],
      Condition = { StringEquals = {
        "aws:RequestTag/kubernetes-namespace"       = local.namespace,
        "aws:RequestTag/kubernetes-service-account" = account,
        "aws:RequestTag/eks-cluster-arn"            = local.cluster_arn
      } }
    }]
  }) }
  system_service_accounts = { vpc_cni = "aws-node", load_balancer_controller = "aws-load-balancer-controller" }
}
resource "aws_iam_role" "workload" {
  for_each           = local.service_accounts
  name               = "${local.name}-${replace(each.key, "_", "-")}"
  assume_role_policy = local.workload_trust_policies[each.key]
}
resource "aws_iam_role_policy" "secrets" {
  for_each = { for key, arns in var.workload_secret_arns : key => arns if length(arns) > 0 }
  role     = aws_iam_role.workload[each.key].name
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    { Effect = "Allow", Action = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"], Resource = tolist(each.value) },
    # Defense in depth even if somebody later attaches a broad IAM policy to this role.
    { Effect = "Deny", Action = ["secretsmanager:GetSecretValue"], Resource = tolist(var.forbidden_secret_arns) }
    ], length(lookup(var.secret_kms_key_arns, each.key, [])) > 0 ? [{
      Effect = "Allow", Action = ["kms:Decrypt"], Resource = tolist(var.secret_kms_key_arns[each.key]), Condition = {
        StringEquals = { "kms:ViaService" = "secretsmanager.${var.region}.amazonaws.com" },
        StringLike   = { "kms:EncryptionContext:SecretARN" = tolist(each.value) }
      }
  }] : []) })
}
resource "aws_iam_role_policy" "media_storage" {
  for_each = toset(["api", "media_worker", "embedding_worker"])
  role     = aws_iam_role.workload[each.key].name
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    { Effect = "Allow", Action = each.key == "embedding_worker" ? ["s3:GetObject"] : ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion"],
    Resource = [for directory in each.key == "api" ? ["staging", "media", "originals", "thumbnails"] : ["staging", "media"] : "${var.media_bucket_arn}/${local.prefix}${directory}/*"] },
    { Effect = "Allow", Action = ["s3:ListBucket", "s3:ListBucketVersions"], Resource = var.media_bucket_arn,
    Condition = { StringLike = { "s3:prefix" = [for directory in each.key == "api" ? ["staging", "media", "originals", "thumbnails"] : ["staging", "media"] : "${local.prefix}${directory}/*"] } } },
    { Effect = "Allow", Action = ["s3:GetBucketVersioning"], Resource = var.media_bucket_arn }
    ], var.media_kms_key_arn == null ? [] : [{
      Effect    = "Allow", Action = each.key == "embedding_worker" ? ["kms:Decrypt"] : ["kms:Decrypt", "kms:GenerateDataKey"], Resource = var.media_kms_key_arn,
      Condition = { StringEquals = { "kms:ViaService" = "s3.${var.region}.amazonaws.com" }, StringLike = { "kms:EncryptionContext:aws:s3:arn" = [var.media_bucket_arn, "${var.media_bucket_arn}/${local.prefix}*"] } }
  }]) })
}
resource "aws_eks_pod_identity_association" "workload" {
  for_each        = local.service_accounts
  cluster_name    = aws_eks_cluster.this.name
  namespace       = local.namespace
  service_account = each.value
  role_arn        = aws_iam_role.workload[each.key].arn
  depends_on      = [aws_eks_addon.bootstrap]
}
resource "aws_iam_role" "system" {
  for_each = local.system_service_accounts
  name     = "${local.name}-${replace(each.key, "_", "-")}"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Principal = { Service = "pods.eks.amazonaws.com" }, Action = ["sts:AssumeRole", "sts:TagSession"],
    Condition = { StringEquals = {
      "aws:RequestTag/kubernetes-namespace"       = "kube-system",
      "aws:RequestTag/kubernetes-service-account" = each.value,
      "aws:RequestTag/eks-cluster-arn"            = local.cluster_arn
    } }
  }] })
}
resource "aws_iam_role_policy_attachment" "vpc_cni" {
  role       = aws_iam_role.system["vpc_cni"].name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AmazonEKS_CNI_Policy"
}
resource "aws_iam_role_policy" "load_balancer_controller" {
  role   = aws_iam_role.system["load_balancer_controller"].name
  policy = file("${path.module}/policies/aws-load-balancer-controller.json")
}
resource "aws_eks_pod_identity_association" "system" {
  for_each        = local.system_service_accounts
  cluster_name    = aws_eks_cluster.this.name
  namespace       = "kube-system"
  service_account = each.value
  role_arn        = aws_iam_role.system[each.key].arn
  depends_on      = [aws_eks_addon.bootstrap, aws_iam_role_policy_attachment.vpc_cni, aws_iam_role_policy.load_balancer_controller]
}

resource "aws_iam_role" "github_deploy" {
  name = "${local.name}-github-deploy"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRoleWithWebIdentity", Principal = { Federated = var.github_oidc_provider_arn },
    Condition = { StringEquals = {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
      "token.actions.githubusercontent.com:sub" = "repo:${var.github_repository}:environment:eks-${var.environment}"
    } }
  }] })
}
resource "aws_iam_role_policy" "github_deploy" {
  role = aws_iam_role.github_deploy.name
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["eks:DescribeCluster", "eks:ListAddons"], Resource = local.cluster_arn },
    { Effect = "Allow", Action = ["eks:DescribeAddon"], Resource = "arn:${local.partition}:eks:${var.region}:${var.expected_account_id}:addon/${local.name}/*" },
    # ELBv2 describes have no resource-level support; constrain read-only discovery to this region.
    { Effect = "Allow", Action = ["elasticloadbalancing:DescribeLoadBalancers", "elasticloadbalancing:DescribeTags", "elasticloadbalancing:DescribeTargetGroups", "elasticloadbalancing:DescribeTargetHealth"], Resource = "*", Condition = { StringEquals = { "aws:RequestedRegion" = var.region } } },
    { Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*" },
    { Effect = "Allow", Action = ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:DescribeImages", "ecr:DescribeRepositories"], Resource = tolist(var.ecr_repository_arns) },
    { Effect = "Allow", Action = ["s3:ListBucket", "s3:GetBucketTagging", "s3:GetBucketVersioning", "s3:GetBucketPublicAccessBlock", "s3:GetEncryptionConfiguration", "s3:GetLifecycleConfiguration"], Resource = var.media_bucket_arn },
    { Effect = "Allow", Action = ["s3:ListBucket"], Resource = var.frontend_bucket_arn },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], Resource = "${var.frontend_bucket_arn}/*" },
    { Effect = "Allow", Action = ["cloudfront:CreateInvalidation", "cloudfront:GetInvalidation", "cloudfront:GetDistribution"], Resource = var.frontend_distribution_arn }
  ] })
}
resource "aws_eks_access_entry" "admin" {
  for_each      = var.admin_principal_arns
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = each.value
  type          = "STANDARD"
}
resource "aws_eks_access_policy_association" "admin" {
  for_each      = var.admin_principal_arns
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = aws_eks_access_entry.admin[each.value].principal_arn
  policy_arn    = "arn:${local.partition}:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
  access_scope { type = "cluster" }
}
resource "aws_eks_access_entry" "github_deploy" {
  cluster_name      = aws_eks_cluster.this.name
  principal_arn     = aws_iam_role.github_deploy.arn
  type              = "STANDARD"
  kubernetes_groups = ["photoplatform-deployers"]
  # Deliberately no managed EKS access policy: bootstrap grants only namespace Role permissions.
}
