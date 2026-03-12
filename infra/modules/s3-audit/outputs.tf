output "audit_bucket_name" {
  description = "Nome do bucket S3 de auditoria WORM"
  value       = aws_s3_bucket.audit.bucket
}

output "audit_bucket_arn" {
  description = "ARN do bucket S3 de auditoria WORM"
  value       = aws_s3_bucket.audit.arn
}

output "error_bucket_name" {
  description = "Nome do bucket S3 de erros do Firehose"
  value       = aws_s3_bucket.errors.bucket
}

output "error_bucket_arn" {
  description = "ARN do bucket S3 de erros do Firehose"
  value       = aws_s3_bucket.errors.arn
}
