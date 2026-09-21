# Provider mocks exercise Terraform graph/expression/security contracts only.
# They do not establish IAM correctness, AWS availability, or cloud acceptance.
mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012", arn = "arn:aws:iam::123456789012:root", user_id = "test" }
  }
  mock_data "aws_partition" { defaults = { partition = "aws", dns_suffix = "amazonaws.com" } }
  mock_data "aws_secretsmanager_secret_version" {
    defaults = { secret_string = "{\"username\":\"photoplatform\",\"password\":\"TestOnlySecurePassword123\"}" }
  }
  mock_data "aws_subnet" { defaults = { vpc_id = "vpc-12345678" } }
}
variables {
  environment            = "dev"
  availability_zones     = ["us-east-1a", "us-east-1b", "us-east-1c"]
  bucket_name_prefix     = "test-photoplatform-123456789012"
  api_domain             = "api.example.com"
  api_certificate_arn    = "arn:aws:acm:us-east-1:123456789012:certificate/12345678-1234-1234-1234-123456789012"
  cdn_public_key_pem     = "test-only-public-key"
  application_secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:app-abcdef"
  mq_secret_arn          = "arn:aws:secretsmanager:us-east-1:123456789012:secret:mq-abcdef"
}
run "bootstrap_keeps_services_off_and_data_private" {
  command = plan
  assert {
    condition     = module.ecs.service_count == 0 && !module.rds.publicly_accessible && !module.mq.publicly_accessible
    error_message = "Bootstrap must provision private infrastructure without starting nonexistent images."
  }
  assert {
    condition     = module.s3.public_access_blocked && module.cloudfront.media_requires_signature
    error_message = "Private media requires both public S3 blocking and signed CloudFront access."
  }
  assert {
    condition     = alltrue([for header in ["if-none-match", "x-amz-meta-upload-id", "x-amz-checksum-sha256", "content-type"] : contains(module.s3.cors_upload_headers, header)])
    error_message = "Browser CORS must permit the exact headers signed by the upload API."
  }
}
run "services_use_private_networks_with_independent_encoder_worker" {
  command = plan
  variables {
    create_services = true
    enable_encoder  = true
    image_refs = {
      api       = "123456789012.dkr.ecr.us-east-1.amazonaws.com/photoplatform-dev/api@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      worker    = "123456789012.dkr.ecr.us-east-1.amazonaws.com/photoplatform-dev/worker@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      collector = "123456789012.dkr.ecr.us-east-1.amazonaws.com/photoplatform-dev/collector@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
      encoder   = "123456789012.dkr.ecr.us-east-1.amazonaws.com/photoplatform-dev/encoder@sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
    }
  }
  assert {
    condition     = module.ecs.service_count == 5 && module.ecs.private_tasks
    error_message = "API, media worker, encoder, embedding worker, and collector must be separate private services."
  }
}
run "reject_services_without_real_image_digests" {
  command = plan
  variables { create_services = true }
  expect_failures = [terraform_data.deployment_requirements]
}
run "reject_backlog_without_collector" {
  command = plan
  variables { enable_collector = false }
  expect_failures = [terraform_data.deployment_requirements]
}

run "reject_production_services_using_bootstrap_credentials" {
  command = plan
  variables {
    environment      = "prod"
    mq_instance_type = "mq.m7g.large"
    create_services  = true
    image_refs = {
      api       = "123456789012.dkr.ecr.us-east-1.amazonaws.com/photoplatform-prod/api@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      worker    = "123456789012.dkr.ecr.us-east-1.amazonaws.com/photoplatform-prod/worker@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      collector = "123456789012.dkr.ecr.us-east-1.amazonaws.com/photoplatform-prod/collector@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    }
  }
  expect_failures = [terraform_data.deployment_requirements]
}
