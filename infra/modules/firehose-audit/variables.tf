variable "stream_name" {
  description = "Nome do Kinesis Data Firehose delivery stream"
  type        = string

  validation {
    condition     = length(var.stream_name) >= 1 && length(var.stream_name) <= 64
    error_message = "O nome do stream deve ter entre 1 e 64 caracteres."
  }
}

variable "audit_bucket_arn" {
  description = "ARN do bucket S3 de destino para auditoria WORM"
  type        = string
}

variable "error_bucket_arn" {
  description = "ARN do bucket S3 de erros do Firehose"
  type        = string
}

variable "glue_database_name" {
  description = "Nome do banco de dados no Glue Catalog"
  type        = string
  default     = "ledger_audit"
}

variable "glue_table_name" {
  description = "Nome da tabela no Glue Catalog (schema do AuditRecord)"
  type        = string
  default     = "audit_records"
}

variable "buffer_size_mb" {
  description = "Tamanho do buffer do Firehose em MB (1-128)"
  type        = number
  default     = 128

  validation {
    condition     = var.buffer_size_mb >= 1 && var.buffer_size_mb <= 128
    error_message = "buffer_size_mb deve estar entre 1 e 128 MB."
  }
}

variable "buffer_interval_seconds" {
  description = "Intervalo do buffer do Firehose em segundos (60-900)"
  type        = number
  default     = 60

  validation {
    condition     = var.buffer_interval_seconds >= 60 && var.buffer_interval_seconds <= 900
    error_message = "buffer_interval_seconds deve estar entre 60 e 900 segundos."
  }
}

variable "tags" {
  description = "Tags aplicadas a todos os recursos do módulo"
  type        = map(string)
  default     = {}
}
