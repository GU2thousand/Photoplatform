terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}

variable "region" { type = string }
variable "bucket_name" { type = string }
variable "frontend_origin" { type = string }
variable "storage_prefix" {
  type        = string
  default     = "generate-cloud"
  description = "Must match STORAGE_PREFIX on the API and all workers; no leading/trailing slash."
  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9/_-]*[A-Za-z0-9]$", var.storage_prefix))
    error_message = "Use a nonempty relative prefix without leading/trailing slashes."
  }
}
variable "cdn_public_key_pem" {
  type        = string
  description = "RSA public key only. Keep the matching private key in the API secret store."
}

provider "aws" { region = var.region }

resource "aws_s3_bucket" "media" { bucket = var.bucket_name }
resource "aws_s3_bucket_ownership_controls" "media" {
  bucket = aws_s3_bucket.media.id
  rule { object_ownership = "BucketOwnerEnforced" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "media" {
  bucket = aws_s3_bucket.media.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}
resource "aws_s3_bucket_public_access_block" "media" {
  bucket                  = aws_s3_bucket.media.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_versioning" "media" {
  bucket = aws_s3_bucket.media.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_cors_configuration" "media" {
  bucket = aws_s3_bucket.media.id
  cors_rule {
    allowed_origins = [var.frontend_origin]
    allowed_methods = ["PUT", "GET", "HEAD"]
    allowed_headers = ["*"]
    expose_headers  = ["ETag", "x-amz-checksum-sha256"]
    max_age_seconds = 300
  }
}
resource "aws_s3_bucket_lifecycle_configuration" "media" {
  depends_on = [aws_s3_bucket_versioning.media]
  bucket     = aws_s3_bucket.media.id
  rule {
    id     = "abandoned-staging"
    status = "Enabled"
    filter { prefix = "${var.storage_prefix}/staging/" }
    expiration { days = 7 }
    noncurrent_version_expiration { noncurrent_days = 7 }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
}
resource "aws_cloudfront_origin_access_control" "media" {
  name                              = "${var.bucket_name}-origin"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}
resource "aws_cloudfront_public_key" "media" {
  encoded_key = var.cdn_public_key_pem
  name        = "${var.bucket_name}-viewer"
}
resource "aws_cloudfront_key_group" "media" {
  name  = "${var.bucket_name}-viewers"
  items = [aws_cloudfront_public_key.media.id]
}
resource "aws_cloudfront_cache_policy" "media" {
  name        = "${var.bucket_name}-immutable-variants"
  default_ttl = 86400
  max_ttl     = 31536000
  min_ttl     = 0
  parameters_in_cache_key_and_forwarded_to_origin {
    cookies_config { cookie_behavior = "none" }
    headers_config { header_behavior = "none" }
    query_strings_config { query_string_behavior = "none" }
  }
}
# Edge objects remain immutable; limit browser freshness independently. A signature
# is checked on each network request, but cannot revoke a copy already downloaded.
resource "aws_cloudfront_response_headers_policy" "media" {
  name = "${var.bucket_name}-viewer-cache"
  custom_headers_config {
    items {
      header   = "Cache-Control"
      value    = "public, max-age=60"
      override = true
    }
    items {
      header   = "X-Content-Type-Options"
      value    = "nosniff"
      override = true
    }
  }
}
resource "aws_cloudfront_distribution" "media" {
  enabled     = true
  price_class = "PriceClass_100"
  origin {
    domain_name              = aws_s3_bucket.media.bucket_regional_domain_name
    origin_id                = "private-media"
    origin_access_control_id = aws_cloudfront_origin_access_control.media.id
  }
  default_cache_behavior {
    target_origin_id           = "private-media"
    viewer_protocol_policy     = "https-only"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = aws_cloudfront_cache_policy.media.id
    trusted_key_groups         = [aws_cloudfront_key_group.media.id]
    response_headers_policy_id = aws_cloudfront_response_headers_policy.media.id
    compress                   = true
  }
  restrictions {
    geo_restriction { restriction_type = "none" }
  }
  viewer_certificate { cloudfront_default_certificate = true }
}
resource "aws_s3_bucket_policy" "cdn" {
  bucket = aws_s3_bucket.media.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "cloudfront.amazonaws.com" }
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.media.arn}/${var.storage_prefix}/media/*"
      Condition = { StringEquals = { "AWS:SourceArn" = aws_cloudfront_distribution.media.arn } }
      }, {
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.media.arn, "${aws_s3_bucket.media.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}
output "cdn_domain" { value = aws_cloudfront_distribution.media.domain_name }
output "cdn_key_pair_id" { value = aws_cloudfront_public_key.media.id }
output "storage_bucket" { value = aws_s3_bucket.media.id }

output "storage_prefix" { value = var.storage_prefix }
