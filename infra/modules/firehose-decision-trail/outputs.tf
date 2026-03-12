# ─────────────────────────────────────────────────────────────────────────────
# outputs.tf — Outputs do módulo firehose-decision-trail
#
# Expõe apenas o necessário para composição no root module.
# Nenhum segredo ou dado sensível é exposto.
# ─────────────────────────────────────────────────────────────────────────────

# ─── Firehose ─────────────────────────────────────────────────────────────────

output "firehose_stream_arn" {
  description = "ARN do Kinesis Data Firehose delivery stream para DecisionTrails"
  value       = aws_kinesis_firehose_delivery_stream.decision_trails.arn
}

output "firehose_stream_name" {
  description = "Nome do Kinesis Data Firehose delivery stream para DecisionTrails"
  value       = aws_kinesis_firehose_delivery_stream.decision_trails.name
}

# ─── S3 ───────────────────────────────────────────────────────────────────────

output "trail_bucket_name" {
  description = "Nome do bucket S3 de destino para os DecisionTrails em Parquet"
  value       = aws_s3_bucket.trails.bucket
}

output "trail_bucket_arn" {
  description = "ARN do bucket S3 de destino para os DecisionTrails"
  value       = aws_s3_bucket.trails.arn
}

output "error_bucket_name" {
  description = "Nome do bucket S3 dedicado para registros de erro do pipeline Firehose"
  value       = aws_s3_bucket.errors.bucket
}

output "error_bucket_arn" {
  description = "ARN do bucket S3 dedicado para registros de erro do pipeline Firehose"
  value       = aws_s3_bucket.errors.arn
}

# ─── Glue ─────────────────────────────────────────────────────────────────────

output "glue_database_name" {
  description = "Nome do banco de dados no Glue Catalog para os DecisionTrails"
  value       = aws_glue_catalog_database.trails.name
}

output "glue_table_name" {
  description = "Nome da tabela no Glue Catalog com o schema do DecisionTrail"
  value       = aws_glue_catalog_table.decision_trails.name
}

# ─── IAM ──────────────────────────────────────────────────────────────────────

output "firehose_role_arn" {
  description = "ARN da IAM role do Firehose (para referência e auditoria)"
  value       = aws_iam_role.firehose.arn
}

output "emitter_policy_arn" {
  description = "ARN da IAM policy de emissão least-privilege. Anexar à role do Data Plane / runtime."
  value       = aws_iam_policy.emitter.arn
}
