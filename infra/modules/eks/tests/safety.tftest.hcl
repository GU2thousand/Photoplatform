# Pure mocked plans: no AWS account, resource provision, or live deployment is exercised.
mock_provider "aws" {
  mock_data "aws_caller_identity" { defaults = { account_id = "123456789012" } }
  mock_data "aws_partition" { defaults = { partition = "aws" } }
  mock_data "aws_vpc" { defaults = { cidr_block = "10.70.0.0/16" } }
  mock_data "aws_subnet" { defaults = { vpc_id = "vpc-0123456789abcdef0", map_public_ip_on_launch = false, availability_zone = "us-east-1a" } }
  mock_data "aws_security_group" { defaults = { vpc_id = "vpc-0123456789abcdef0" } }
}
override_data {
  target = data.aws_subnet.private["subnet-0123456789abcdef1"]
  values = { vpc_id = "vpc-0123456789abcdef0", map_public_ip_on_launch = false, availability_zone = "us-east-1b" }
}
override_data {
  target = data.aws_subnet.public["subnet-0123456789abcdef3"]
  values = { vpc_id = "vpc-0123456789abcdef0", map_public_ip_on_launch = false, availability_zone = "us-east-1b" }
}
variables {
  environment                   = "dev"
  network_policy_enforcing_mode = "strict"
  region                        = "us-east-1"
  expected_account_id           = "123456789012"
  vpc_id                        = "vpc-0123456789abcdef0"
  private_subnet_ids            = ["subnet-0123456789abcdef0", "subnet-0123456789abcdef1"]
  public_subnet_ids             = ["subnet-0123456789abcdef2", "subnet-0123456789abcdef3"]
  runner_security_group_ids     = ["sg-0123456789abcdef0"]
  dependency_security_group_ids = { rds = "sg-0123456789abcdef1", mq = "sg-0123456789abcdef2" }
  admin_principal_arns          = ["arn:aws:iam::123456789012:role/PlatformAdministrator"]
  github_oidc_provider_arn      = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
  ecr_repository_arns           = ["arn:aws:ecr:us-east-1:123456789012:repository/photoplatform-dev/api", "arn:aws:ecr:us-east-1:123456789012:repository/photoplatform-dev/worker"]
  media_bucket_arn              = "arn:aws:s3:::photoplatform-fixture-media"
  storage_prefix                = "photoplatform-dev"
  frontend_bucket_arn           = "arn:aws:s3:::photoplatform-fixture-frontend"
  frontend_distribution_arn     = "arn:aws:cloudfront::123456789012:distribution/EXAMPLE"
  forbidden_secret_arns         = ["arn:aws:secretsmanager:us-east-1:123456789012:secret:admin-AbCdEf"]
  workload_secret_arns = {
    api              = ["arn:aws:secretsmanager:us-east-1:123456789012:secret:app-AbCdEf"]
    media_worker     = ["arn:aws:secretsmanager:us-east-1:123456789012:secret:runtime-AbCdEf"]
    embedding_worker = []
    encoder          = []
    migrator         = ["arn:aws:secretsmanager:us-east-1:123456789012:secret:migrator-AbCdEf"]
    queue_collector  = []
  }
  # Fixtures exercise exact-version syntax only; not a regional compatibility claim.
  addon_versions           = { vpc_cni = "v1.23.0-eksbuild.1", coredns = "v1.13.0-eksbuild.1", kube_proxy = "v1.36.0-eksbuild.1", pod_identity_agent = "v1.3.0-eksbuild.1" }
  node_ami_release_version = "1.36.0-20260901"
}
run "private_independent_compute" {
  command = plan
  assert {
    condition     = aws_eks_cluster.this.vpc_config[0].endpoint_private_access && !aws_eks_cluster.this.vpc_config[0].endpoint_public_access
    error_message = "Cluster administration must have no public endpoint."
  }
  assert {
    condition     = aws_eks_cluster.this.access_config[0].authentication_mode == "API" && !aws_eks_cluster.this.access_config[0].bootstrap_cluster_creator_admin_permissions
    error_message = "Access entries must be explicit, without implicit creator administration."
  }
  assert {
    condition     = aws_eks_node_group.this["application"].scaling_config[0].min_size >= 2 && aws_eks_node_group.this["ml"].scaling_config[0].desired_size == 0
    error_message = "Keep two application nodes; CPU ML remains opt-in."
  }
  assert {
    condition     = aws_launch_template.node.metadata_options[0].http_tokens == "required" && aws_launch_template.node.metadata_options[0].http_put_response_hop_limit == 1
    error_message = "Workloads must not silently inherit node IMDS credentials."
  }
  assert {
    condition     = jsondecode(aws_eks_addon.bootstrap["vpc-cni"].configuration_values).enableNetworkPolicy == "true" && jsondecode(aws_eks_addon.bootstrap["vpc-cni"].configuration_values).env.NETWORK_POLICY_ENFORCING_MODE == "strict"
    error_message = "NetworkPolicy needs actual strict CNI enforcement configuration."
  }
  assert {
    condition     = contains(aws_eks_access_entry.github_deploy.kubernetes_groups, "photoplatform-deployers") && length(aws_eks_access_policy_association.admin) == 1
    error_message = "Release access must stay separate from the named platform administrator."
  }
  assert {
    condition     = jsondecode(aws_iam_role.workload["encoder"].assume_role_policy).Statement[0].Condition.StringEquals["aws:RequestTag/kubernetes-service-account"] == "photoplatform-encoder" && !contains(keys(aws_iam_role_policy.media_storage), "encoder")
    error_message = "Encoder identity must be scoped and have no S3 grant."
  }
  assert {
    condition     = alltrue([for statement in jsondecode(aws_iam_role_policy.media_storage["embedding_worker"].policy).Statement : !contains(statement.Action, "s3:PutObject") && !contains(statement.Action, "s3:DeleteObject")])
    error_message = "Embedding must read source media without media mutation grants."
  }
  assert {
    condition     = jsondecode(aws_iam_role.github_deploy.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:GU2thousand/Photoplatform:environment:eks-dev"
    error_message = "OIDC release identity must match repository and protected environment."
  }
}
run "wrong_account_rejected" {
  command = plan
  override_data {
    target = data.aws_caller_identity.current
    values = { account_id = "999999999999" }
  }
  expect_failures = [terraform_data.requirements]
}
run "single_az_rejected" {
  command = plan
  override_data {
    target = data.aws_subnet.private["subnet-0123456789abcdef1"]
    values = { vpc_id = "vpc-0123456789abcdef0", map_public_ip_on_launch = false, availability_zone = "us-east-1a" }
  }
  expect_failures = [terraform_data.requirements]
}
run "admin_secret_rejected" {
  command = plan
  variables { forbidden_secret_arns = ["arn:aws:secretsmanager:us-east-1:123456789012:secret:app-AbCdEf"] }
  expect_failures = [terraform_data.requirements]
}
run "migrator_shared_secret_rejected" {
  command = plan
  variables {
    workload_secret_arns = {
      api              = ["arn:aws:secretsmanager:us-east-1:123456789012:secret:runtime-AbCdEf"]
      media_worker     = ["arn:aws:secretsmanager:us-east-1:123456789012:secret:runtime-AbCdEf"]
      embedding_worker = [], encoder = [], queue_collector = []
      migrator         = ["arn:aws:secretsmanager:us-east-1:123456789012:secret:runtime-AbCdEf"]
    }
  }
  expect_failures = [terraform_data.requirements]
}
run "wildcard_prefix_rejected" {
  command = plan
  variables { storage_prefix = "*" }
  expect_failures = [var.storage_prefix]
}
run "unpinned_addon_rejected" {
  command = plan
  variables { addon_versions = { vpc_cni = "latest", coredns = "v1.13.0-eksbuild.1", kube_proxy = "v1.36.0-eksbuild.1", pod_identity_agent = "v1.3.0-eksbuild.1" } }
  expect_failures = [var.addon_versions]
}
run "prod_cannot_be_disposable" {
  command = plan
  variables {
    environment            = "prod"
    disposable_environment = true
  }
  expect_failures = [terraform_data.requirements]
}
run "release_cannot_be_admin" {
  command = plan
  variables { admin_principal_arns = ["arn:aws:iam::123456789012:role/photoplatform-eks-dev-github-deploy"] }
  expect_failures = [terraform_data.requirements]
}
run "wildcard_kms_rejected" {
  command = plan
  variables { secret_kms_key_arns = { api = ["*"] } }
  expect_failures = [var.secret_kms_key_arns]
}
run "cross_region_secret_rejected" {
  command = plan
  variables { forbidden_secret_arns = ["arn:aws:secretsmanager:us-west-2:123456789012:secret:admin-AbCdEf"] }
  expect_failures = [terraform_data.requirements]
}
