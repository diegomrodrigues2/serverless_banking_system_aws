output "event_bus_name" {
  description = "Nome do Event Bus do EventBridge"
  value       = aws_cloudwatch_event_bus.ledger.name
}

output "event_bus_arn" {
  description = "ARN do Event Bus do EventBridge"
  value       = aws_cloudwatch_event_bus.ledger.arn
}

output "transaction_created_rule_arn" {
  description = "ARN da rule para eventos TransactionCreated"
  value       = aws_cloudwatch_event_rule.transaction_created.arn
}

output "transaction_reversed_rule_arn" {
  description = "ARN da rule para eventos TransactionReversed"
  value       = aws_cloudwatch_event_rule.transaction_reversed.arn
}
