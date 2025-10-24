resource "aws_s3_bucket_server_side_encryption_configuration" "facts" {
  bucket = aws_s3_bucket.facts.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}
