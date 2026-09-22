variable "name" { type = string }
variable "frontend_origins" { type = list(string) }
variable "storage_prefix" { type = string }
variable "force_destroy" { type = bool }

locals {
  prefix = var.storage_prefix == "" ? "" : "${trim(var.storage_prefix, "/")}/"
}

resource "aws_s3_bucket" "buckets" {
  for_each      = toset(["media", "frontend"])
  bucket        = "${var.name}-${each.key}"
  force_destroy = var.force_destroy
}
resource "aws_s3_bucket_public_access_block" "buckets" {
  for_each                = aws_s3_bucket.buckets
  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_ownership_controls" "buckets" {
  for_each = aws_s3_bucket.buckets
  bucket   = each.value.id
  rule { object_ownership = "BucketOwnerEnforced" }
}
resource "aws_s3_bucket_versioning" "buckets" {
  for_each = aws_s3_bucket.buckets
  bucket   = each.value.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "buckets" {
  for_each = aws_s3_bucket.buckets
  bucket   = each.value.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}
resource "aws_s3_bucket_cors_configuration" "media" {
  bucket = aws_s3_bucket.buckets["media"].id
  cors_rule {
    allowed_origins = var.frontend_origins
    allowed_methods = ["PUT", "GET", "HEAD"]
    allowed_headers = ["content-type", "x-amz-checksum-sha256", "x-amz-sdk-checksum-algorithm", "x-amz-meta-sha256", "x-amz-meta-upload-id", "if-none-match"]
    expose_headers  = ["ETag", "x-amz-checksum-sha256"]
    max_age_seconds = 300
  }
}
resource "aws_s3_bucket_lifecycle_configuration" "media" {
  bucket     = aws_s3_bucket.buckets["media"].id
  depends_on = [aws_s3_bucket_versioning.buckets]
  rule {
    id     = "abandoned-staging-one-day"
    status = "Enabled"
    filter { prefix = "${local.prefix}staging/" }
    expiration { days = 1 }
    noncurrent_version_expiration { noncurrent_days = 1 }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
  rule {
    id     = "remove-staging-delete-markers"
    status = "Enabled"
    filter { prefix = "${local.prefix}staging/" }
    expiration { expired_object_delete_marker = true }
  }
  rule {
    id     = "media-noncurrent-retention"
    status = "Enabled"
    filter { prefix = "${local.prefix}media/" }
    noncurrent_version_expiration { noncurrent_days = 30 }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
}
resource "aws_s3_bucket_lifecycle_configuration" "frontend" {
  bucket     = aws_s3_bucket.buckets["frontend"].id
  depends_on = [aws_s3_bucket_versioning.buckets]
  rule {
    id     = "old-deployments"
    status = "Enabled"
    filter { prefix = "" }
    noncurrent_version_expiration { noncurrent_days = 7 }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
}
output "buckets" {
  value = { for name, bucket in aws_s3_bucket.buckets : name => {
    name = bucket.id, arn = bucket.arn, domain_name = bucket.bucket_regional_domain_name
  } }
}

output "cors_upload_headers" { value = one(aws_s3_bucket_cors_configuration.media.cors_rule).allowed_headers }
output "public_access_blocked" { value = alltrue([for block in aws_s3_bucket_public_access_block.buckets : block.block_public_acls && block.block_public_policy && block.ignore_public_acls && block.restrict_public_buckets]) }
