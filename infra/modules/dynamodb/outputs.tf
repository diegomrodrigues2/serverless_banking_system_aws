output "table_name" {
  description = "Nome da tabela DynamoDB"
  value       = aws_dynamodb_table.ledger.name
}

output "table_arn" {
  description = "ARN da tabela DynamoDB"
  value       = aws_dynamodb_table.ledger.arn
}

output "stream_arn" {
  description = "ARN do DynamoDB Stream (usado pelos Event Source Mappings das Lambdas)"
  value       = aws_dynamodb_table.ledger.stream_arn
}
