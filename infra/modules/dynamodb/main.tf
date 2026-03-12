# ─────────────────────────────────────────────────────────────────────────────
# Módulo DynamoDB — Single-Table Design para o Double-Entry Ledger
#
# Cria a tabela principal com:
#   - PK (string) + SK (string) — single-table design
#   - PITR habilitado para recuperação point-in-time
#   - DynamoDB Streams (NEW_IMAGE) para Outbox e Audit pipelines
#   - TTL no campo expires_at para limpeza automática de OutboxEvents
#   - GSI-EntryPostings para busca de postings por journal entry
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_dynamodb_table" "ledger" {
  name         = var.table_name
  billing_mode = var.billing_mode
  hash_key     = "PK"
  range_key    = "SK"

  # Atributos usados como chaves (PK, SK e atributos dos GSIs)
  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }

  # entry_id_gsi: atributo de partição do GSI-EntryPostings
  # Armazenado nos itens de Posting como "JOURNAL#{entry_id}"
  attribute {
    name = "entry_id_gsi"
    type = "S"
  }

  # GSI-EntryPostings: busca todos os postings de um journal entry por entry_id_gsi
  # hash_key: entry_id_gsi = "JOURNAL#{entry_id}"
  global_secondary_index {
    name            = "GSI-EntryPostings"
    hash_key        = "entry_id_gsi"
    projection_type = "ALL"
  }

  # Point-in-Time Recovery — obrigatório para dados financeiros
  point_in_time_recovery {
    enabled = true
  }

  # DynamoDB Streams — captura NEW_IMAGE para Outbox e Audit pipelines
  stream_enabled   = true
  stream_view_type = var.stream_view_type

  # TTL — limpeza automática de OutboxEvents após processamento
  ttl {
    attribute_name = var.ttl_attribute
    enabled        = true
  }

  tags = merge(var.tags, {
    Module = "dynamodb"
  })
}
