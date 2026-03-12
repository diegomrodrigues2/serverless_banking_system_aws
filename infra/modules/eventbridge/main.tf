# ─────────────────────────────────────────────────────────────────────────────
# Módulo EventBridge — Barramento de eventos do Double-Entry Ledger
#
# Cria:
#   1. Event Bus dedicado para eventos do ledger
#   2. Rules para roteamento de TransactionCreated e TransactionReversed
#
# Os targets (SQS, Lambda, SNS) são configurados via variáveis para permitir
# que consumidores downstream se conectem sem modificar este módulo.
# ─────────────────────────────────────────────────────────────────────────────

# ─── Event Bus dedicado para o ledger ────────────────────────────────────────

resource "aws_cloudwatch_event_bus" "ledger" {
  name = var.event_bus_name

  tags = merge(var.tags, { Module = "eventbridge" })
}

# ─── Rule: TransactionCreated ─────────────────────────────────────────────────

resource "aws_cloudwatch_event_rule" "transaction_created" {
  name           = "${var.event_bus_name}-transaction-created"
  description    = "Roteia eventos TransactionCreated para consumidores downstream"
  event_bus_name = aws_cloudwatch_event_bus.ledger.name

  # Filtra apenas eventos do tipo TransactionCreated publicados pelo Publisher Lambda
  event_pattern = jsonencode({
    source      = ["ledger.transactions"]
    detail-type = ["TransactionCreated"]
  })

  tags = merge(var.tags, { Module = "eventbridge", EventType = "TransactionCreated" })
}

# ─── Rule: TransactionReversed ────────────────────────────────────────────────

resource "aws_cloudwatch_event_rule" "transaction_reversed" {
  name           = "${var.event_bus_name}-transaction-reversed"
  description    = "Roteia eventos TransactionReversed para consumidores downstream"
  event_bus_name = aws_cloudwatch_event_bus.ledger.name

  event_pattern = jsonencode({
    source      = ["ledger.transactions"]
    detail-type = ["TransactionReversed"]
  })

  tags = merge(var.tags, { Module = "eventbridge", EventType = "TransactionReversed" })
}

# ─── Targets opcionais (configurados quando ARNs são fornecidos) ──────────────

resource "aws_cloudwatch_event_target" "transaction_created" {
  count = var.transaction_created_target_arn != "" ? 1 : 0

  rule           = aws_cloudwatch_event_rule.transaction_created.name
  event_bus_name = aws_cloudwatch_event_bus.ledger.name
  target_id      = "transaction-created-target"
  arn            = var.transaction_created_target_arn
}

resource "aws_cloudwatch_event_target" "transaction_reversed" {
  count = var.transaction_reversed_target_arn != "" ? 1 : 0

  rule           = aws_cloudwatch_event_rule.transaction_reversed.name
  event_bus_name = aws_cloudwatch_event_bus.ledger.name
  target_id      = "transaction-reversed-target"
  arn            = var.transaction_reversed_target_arn
}
