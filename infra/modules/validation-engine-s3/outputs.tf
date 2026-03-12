# ─────────────────────────────────────────────────────────────────────────────
# outputs.tf — Outputs do módulo validation-engine-s3
#
# Expõe apenas o necessário para composição no root module.
# Nenhum segredo ou dado sensível é exposto.
# ─────────────────────────────────────────────────────────────────────────────

# ─── Bucket principal (bundles + snapshots) ───────────────────────────────────

output "bundles_bucket_name" {
  description = "Nome do bucket S3 principal para RuleBundles e ReferenceSnapshots"
  value       = aws_s3_bucket.bundles.bucket
}

output "bundles_bucket_arn" {
  description = "ARN do bucket S3 principal para RuleBundles e ReferenceSnapshots"
  value       = aws_s3_bucket.bundles.arn
}

# ─── Bucket de erros ──────────────────────────────────────────────────────────

output "error_bucket_name" {
  description = "Nome do bucket S3 dedicado para registros de erro"
  value       = aws_s3_bucket.errors.bucket
}

output "error_bucket_arn" {
  description = "ARN do bucket S3 dedicado para registros de erro"
  value       = aws_s3_bucket.errors.arn
}

# ─── IAM Policies ─────────────────────────────────────────────────────────────

output "reader_policy_arn" {
  description = "ARN da IAM policy de leitura least-privilege (bundles/ e snapshots/). Anexar à role do Data Plane / runtime."
  value       = aws_iam_policy.reader.arn
}

output "writer_policy_arn" {
  description = "ARN da IAM policy de escrita least-privilege (bundles/ e snapshots/). Anexar à role do Control Plane."
  value       = aws_iam_policy.writer.arn
}
