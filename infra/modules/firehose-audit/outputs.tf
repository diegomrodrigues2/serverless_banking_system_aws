output "firehose_stream_arn" {
  description = "ARN do Kinesis Data Firehose delivery stream"
  value       = aws_kinesis_firehose_delivery_stream.audit.arn
}

output "firehose_stream_name" {
  description = "Nome do Kinesis Data Firehose delivery stream"
  value       = aws_kinesis_firehose_delivery_stream.audit.name
}

output "glue_database_name" {
  description = "Nome do banco de dados no Glue Catalog"
  value       = aws_glue_catalog_database.audit.name
}

output "glue_table_name" {
  description = "Nome da tabela no Glue Catalog"
  value       = aws_glue_catalog_table.audit_records.name
}

output "firehose_role_arn" {
  description = "ARN da IAM role do Firehose"
  value       = aws_iam_role.firehose.arn
}
