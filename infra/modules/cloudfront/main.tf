variable "name" { type = string }
variable "buckets" { type = map(object({ name = string, arn = string, domain_name = string })) }
variable "public_key_pem" { type = string }
variable "storage_prefix" { type = string }

resource "aws_cloudfront_origin_access_control" "buckets" {
  for_each                          = var.buckets
  name                              = "${var.name}-${each.key}"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}
resource "aws_cloudfront_public_key" "media" {
  name        = "${var.name}-viewer"
  encoded_key = var.public_key_pem
}
resource "aws_cloudfront_key_group" "media" {
  name  = "${var.name}-viewers"
  items = [aws_cloudfront_public_key.media.id]
}
resource "aws_cloudfront_cache_policy" "media" {
  name        = "${var.name}-media"
  default_ttl = 60
  max_ttl     = 60
  min_ttl     = 0
  parameters_in_cache_key_and_forwarded_to_origin {
    cookies_config { cookie_behavior = "none" }
    headers_config { header_behavior = "none" }
    query_strings_config { query_string_behavior = "none" }
  }
}
resource "aws_cloudfront_response_headers_policy" "media" {
  name = "${var.name}-media-private-cache"
  custom_headers_config {
    items {
      header   = "Cache-Control"
      value    = "private, no-store"
      override = true
    }
    items {
      header   = "X-Content-Type-Options"
      value    = "nosniff"
      override = true
    }
  }
}
# Rewrite only SPA navigation. Missing static assets continue to return errors;
# never map media authorization errors to the frontend index document.
resource "aws_cloudfront_function" "spa" {
  name    = "${var.name}-spa"
  runtime = "cloudfront-js-2.0"
  publish = true
  code    = <<-JS
    function handler(event) {
      var request = event.request;
      var leaf = request.uri.split('/').pop();
      if (request.uri.endsWith('/') || leaf.indexOf('.') === -1) {
        request.uri = '/index.html';
      }
      return request;
    }
  JS
}
resource "aws_cloudfront_distribution" "buckets" {
  for_each            = var.buckets
  enabled             = true
  price_class         = "PriceClass_100"
  default_root_object = each.key == "frontend" ? "index.html" : null
  origin {
    domain_name              = each.value.domain_name
    origin_id                = each.key
    origin_access_control_id = aws_cloudfront_origin_access_control.buckets[each.key].id
  }
  default_cache_behavior {
    target_origin_id           = each.key
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = each.key == "media" ? aws_cloudfront_cache_policy.media.id : "658327ea-f89d-4fab-a63d-7e88639e58f6"
    trusted_key_groups         = each.key == "media" ? [aws_cloudfront_key_group.media.id] : []
    response_headers_policy_id = each.key == "media" ? aws_cloudfront_response_headers_policy.media.id : "67f7725c-6f97-4210-82d7-5512b31e9d03"
    compress                   = true
    dynamic "function_association" {
      for_each = each.key == "frontend" ? [1] : []
      content {
        event_type   = "viewer-request"
        function_arn = aws_cloudfront_function.spa.arn
      }
    }
  }
  custom_error_response {
    error_code            = 403
    error_caching_min_ttl = 0
  }
  custom_error_response {
    error_code            = 404
    error_caching_min_ttl = 0
  }
  restrictions {
    geo_restriction { restriction_type = "none" }
  }
  viewer_certificate { cloudfront_default_certificate = true }
}
resource "aws_s3_bucket_policy" "buckets" {
  for_each = var.buckets
  bucket   = each.value.name
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    {
      Sid       = "CloudFrontOriginAccess", Effect = "Allow", Principal = { Service = "cloudfront.amazonaws.com" }, Action = "s3:GetObject",
      Resource  = each.key == "media" ? "${each.value.arn}/${var.storage_prefix == "" ? "" : "${trim(var.storage_prefix, "/")}/"}media/*" : "${each.value.arn}/*",
      Condition = { StringEquals = { "AWS:SourceArn" = aws_cloudfront_distribution.buckets[each.key].arn } }
    },
    {
      Sid      = "DenyInsecureTransport", Effect = "Deny", Principal = "*", Action = "s3:*",
      Resource = [each.value.arn, "${each.value.arn}/*"], Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }
  ] })
}
output "distributions" {
  value = { for name, distribution in aws_cloudfront_distribution.buckets : name => {
    id = distribution.id, arn = distribution.arn, domain_name = distribution.domain_name
  } }
}
output "key_pair_id" { value = aws_cloudfront_public_key.media.id }

output "media_requires_signature" { value = length(aws_cloudfront_distribution.buckets["media"].default_cache_behavior[0].trusted_key_groups) > 0 }
