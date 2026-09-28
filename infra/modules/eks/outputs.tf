output "cluster_name" { value = aws_eks_cluster.this.name }
output "cluster_arn" { value = aws_eks_cluster.this.arn }
output "cluster_endpoint" { value = aws_eks_cluster.this.endpoint }
output "cluster_version" { value = aws_eks_cluster.this.version }
output "namespace" { value = local.namespace }
output "vpc_id" { value = var.vpc_id }
output "public_subnet_ids" { value = var.public_subnet_ids }
output "node_security_group_id" { value = aws_security_group.node.id }
output "alb_security_group_id" { value = aws_security_group.alb.id }
output "github_deploy_role_arn" { value = aws_iam_role.github_deploy.arn }
output "workload_role_arns" { value = { for key, role in aws_iam_role.workload : key => role.arn } }
output "system_role_arns" { value = { for key, role in aws_iam_role.system : key => role.arn } }
output "service_accounts" { value = local.service_accounts }
output "node_group_names" { value = { for key, group in aws_eks_node_group.this : key => group.node_group_name } }
output "addon_versions" { value = var.addon_versions }
output "node_ami_release_version" { value = var.node_ami_release_version }
