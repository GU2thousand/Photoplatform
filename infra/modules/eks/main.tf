data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_vpc" "shared" { id = var.vpc_id }
data "aws_subnet" "private" {
  for_each = var.private_subnet_ids
  id       = each.value
}
data "aws_subnet" "public" {
  for_each = var.public_subnet_ids
  id       = each.value
}
data "aws_security_group" "dependency" {
  for_each = var.dependency_security_group_ids
  id       = each.value
}
data "aws_security_group" "runner" {
  for_each = var.runner_security_group_ids
  id       = each.value
}

locals {
  name        = "photoplatform-eks-${var.environment}"
  namespace   = "photoplatform-${var.environment}"
  partition   = data.aws_partition.current.partition
  cluster_arn = "arn:${local.partition}:eks:${var.region}:${var.expected_account_id}:cluster/${local.name}"
  prefix      = "${trim(var.storage_prefix, "/")}/"
  service_accounts = {
    api     = "photoplatform-api", media_worker = "photoplatform-media-worker", embedding_worker = "photoplatform-embedding-worker",
    encoder = "photoplatform-encoder", migrator = "photoplatform-migrator", queue_collector = "photoplatform-queue-collector"
  }
  bootstrap_addons = {
    vpc-cni                = var.addon_versions.vpc_cni
    kube-proxy             = var.addon_versions.kube_proxy
    eks-pod-identity-agent = var.addon_versions.pod_identity_agent
  }
}

resource "terraform_data" "requirements" {
  lifecycle {
    precondition {
      condition     = data.aws_caller_identity.current.account_id == var.expected_account_id
      error_message = "Refuse resources in an unexpected AWS account."
    }
    precondition {
      condition     = !var.disposable_environment || var.environment == "dev"
      error_message = "Only independent dev infrastructure may carry a disposable tag."
    }
    precondition {
      condition     = !contains(var.admin_principal_arns, "arn:${local.partition}:iam::${var.expected_account_id}:role/${local.name}-github-deploy")
      error_message = "The app release role must not be a cluster administrator."
    }
    precondition {
      condition = (alltrue([for subnet in data.aws_subnet.private : subnet.vpc_id == var.vpc_id && !subnet.map_public_ip_on_launch]) &&
      length(toset([for subnet in data.aws_subnet.private : subnet.availability_zone])) >= 2)
      error_message = "Every node subnet must be private in the declared VPC, spanning at least two AZs. Verify NAT/endpoints and route tables in the resource inventory."
    }
    precondition {
      condition     = alltrue([for group in concat(values(data.aws_security_group.dependency), values(data.aws_security_group.runner)) : group.vpc_id == var.vpc_id])
      error_message = "Dependency and runner security groups must belong to the declared VPC."
    }
    precondition {
      condition = (alltrue([for subnet in data.aws_subnet.public : subnet.vpc_id == var.vpc_id]) &&
        length(toset([for subnet in data.aws_subnet.public : subnet.availability_zone])) >= 2 &&
      length(setintersection(var.private_subnet_ids, var.public_subnet_ids)) == 0)
      error_message = "ALB requires distinct existing public subnets across two AZs in the environment VPC."
    }
    precondition {
      condition     = length(setintersection(var.forbidden_secret_arns, toset(flatten([for arns in values(var.workload_secret_arns) : tolist(arns)])))) == 0
      error_message = "Application and migration workloads must never read administrator/bootstrap Secrets."
    }
    precondition {
      condition     = alltrue([for key, arns in var.workload_secret_arns : key == "migrator" || length(setintersection(arns, var.workload_secret_arns.migrator)) == 0])
      error_message = "The DDL migrator Secret must be separate from every runtime Secret allowlist."
    }
    precondition {
      condition = alltrue([for arn in concat(
        tolist(var.admin_principal_arns), [var.github_oidc_provider_arn], tolist(var.ecr_repository_arns),
        flatten([for arns in values(var.workload_secret_arns) : tolist(arns)]), tolist(var.forbidden_secret_arns),
        [var.frontend_distribution_arn], flatten([for arns in values(var.secret_kms_key_arns) : tolist(arns)]),
        var.media_kms_key_arn == null ? [] : [var.media_kms_key_arn]
      ) : split(":", arn)[4] == var.expected_account_id])
      error_message = "Roles, OIDC provider, ECR and Secrets must be in the pinned environment account. Cross-account access requires a separately reviewed design."
    }
    precondition {
      condition = alltrue([for arn in concat(
        tolist(var.ecr_repository_arns), flatten([for arns in values(var.workload_secret_arns) : tolist(arns)]),
        tolist(var.forbidden_secret_arns), flatten([for arns in values(var.secret_kms_key_arns) : tolist(arns)]),
        var.media_kms_key_arn == null ? [] : [var.media_kms_key_arn]
      ) : split(":", arn)[3] == var.region])
      error_message = "Repositories, Secrets and KMS keys must belong to the pinned environment region."
    }
  }
}

resource "aws_kms_key" "cluster" {
  description             = "EKS ${local.name} Kubernetes API data encryption"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Sid = "EnableAccountIAMPolicies", Effect = "Allow", Principal = { AWS = "arn:${local.partition}:iam::${var.expected_account_id}:root" }, Action = "kms:*", Resource = "*"
  }] })
  lifecycle { prevent_destroy = true }
}
resource "aws_kms_alias" "cluster" {
  name          = "alias/${local.name}"
  target_key_id = aws_kms_key.cluster.key_id
}
resource "aws_cloudwatch_log_group" "cluster" {
  name              = "/aws/eks/${local.name}/cluster"
  retention_in_days = var.log_retention_days
}
resource "aws_iam_role" "cluster" {
  name = "${local.name}-cluster"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Principal = { Service = "eks.amazonaws.com" }, Action = "sts:AssumeRole"
  }] })
}
resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AmazonEKSClusterPolicy"
}
resource "aws_eks_cluster" "this" {
  name                          = local.name
  role_arn                      = aws_iam_role.cluster.arn
  version                       = var.kubernetes_version
  bootstrap_self_managed_addons = false
  enabled_cluster_log_types     = ["api", "audit", "authenticator", "controllerManager", "scheduler"]
  deletion_protection           = var.environment == "prod"
  upgrade_policy { support_type = "STANDARD" }
  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = false
  }
  vpc_config {
    subnet_ids              = var.private_subnet_ids
    security_group_ids      = [aws_security_group.control_plane.id]
    endpoint_private_access = true
    endpoint_public_access  = false
  }
  encryption_config {
    resources = ["secrets"]
    provider { key_arn = aws_kms_key.cluster.arn }
  }
  depends_on = [terraform_data.requirements, aws_iam_role_policy_attachment.cluster, aws_cloudwatch_log_group.cluster]
}

resource "aws_iam_role" "node" {
  name = "${local.name}-node"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole"
  }] })
}
resource "aws_iam_role_policy_attachment" "node" {
  for_each   = toset(["AmazonEKSWorkerNodePolicy", "AmazonEC2ContainerRegistryPullOnly"])
  role       = aws_iam_role.node.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/${each.key}"
}
resource "aws_launch_template" "node" {
  name_prefix            = "${local.name}-"
  vpc_security_group_ids = [aws_security_group.node.id]
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "disabled"
  }
  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      encrypted             = true
      delete_on_termination = true
      volume_size           = 50
      volume_type           = "gp3"
    }
  }
  tag_specifications {
    resource_type = "instance"
    tags          = { Name = local.name, "kubernetes.io/cluster/${local.name}" = "owned" }
  }
  lifecycle { create_before_destroy = true }
}
resource "aws_eks_node_group" "this" {
  for_each        = var.node_groups
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "${local.name}-${each.key}"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = var.private_subnet_ids
  ami_type        = "AL2023_x86_64_STANDARD"
  release_version = var.node_ami_release_version
  version         = var.kubernetes_version
  capacity_type   = "ON_DEMAND"
  instance_types  = each.value.instance_types
  labels          = { "photoplatform.io/workload" = each.value.workload }
  dynamic "taint" {
    for_each = each.value.workload == "ml" ? [1] : []
    content {
      key    = "photoplatform.io/workload"
      value  = "ml"
      effect = "NO_SCHEDULE"
    }
  }
  scaling_config {
    min_size     = each.value.min_size
    desired_size = each.value.desired_size
    max_size     = each.value.max_size
  }
  update_config { max_unavailable = 1 }
  launch_template {
    id      = aws_launch_template.node.id
    version = tostring(aws_launch_template.node.latest_version)
  }
  depends_on = [aws_eks_addon.bootstrap, aws_eks_pod_identity_association.system, aws_iam_role_policy_attachment.node]
}

resource "aws_eks_addon" "bootstrap" {
  for_each                    = local.bootstrap_addons
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = each.key
  addon_version               = each.value
  resolve_conflicts_on_create = "NONE"
  resolve_conflicts_on_update = "NONE"
  # CoreDNS bootstrap must precede strict mode: its first Pods need API connectivity before policies exist.
  configuration_values = each.key == "vpc-cni" ? jsonencode({
    enableNetworkPolicy = "true"
    env                 = { NETWORK_POLICY_ENFORCING_MODE = var.network_policy_enforcing_mode }
  }) : null
}
resource "aws_eks_addon" "coredns" {
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "coredns"
  addon_version               = var.addon_versions.coredns
  resolve_conflicts_on_create = "NONE"
  resolve_conflicts_on_update = "NONE"
  depends_on                  = [aws_eks_node_group.this]
}
