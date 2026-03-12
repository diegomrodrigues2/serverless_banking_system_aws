# ─────────────────────────────────────────────────────────────────────────────
# variables.tf — Parâmetros do módulo firehose-decision-trail
#
# Parâmetros para provisionamento do Firehose, Glue e S3 para o pipeline
# de DecisionTrail do Validation Engine.
#
# Requisitos cobertos: 13.5, 21.1, 21.3, 21.4, 21.5, 22.3, 22.7
# ─────────────────────────────────────────────────────────────────────────────

# ─── Identificação ────────────────────────────────────────────────────────────

variable "stream_name" {
  description = "Nome do Kinesis Data Firehose delivery stream para DecisionTrails."
  type        = string

  validation {
    condition     = length(var.stream_name) >= 1 && length(var.stream_name) <= 64
    error_message = "stream_name deve ter entre 1 e 64 caracteres."
  }
}

# ─── S3 ───────────────────────────────────────────────────────────────────────

variable "trail_bucket_name" {
  description = "Nome do bucket S3 de destino para os DecisionTrails em Parquet."
  type        = string

  validation {
    condition     = length(var.trail_bucket_name) >= 3 && length(var.trail_bucket_name) <= 63
    error_message = "trail_bucket_name deve ter entre 3 e 63 caracteres."
  }
}

variable "error_bucket_name" {
  description = "Nome do bucket S3 dedicado para registros de erro do pipeline Firehose."
  type        = string

  validation {
    condition     = length(var.error_bucket_name) >= 3 && length(var.error_bucket_name) <= 63
    error_message = "error_bucket_name deve ter entre 3 e 63 caracteres."
  }
}

# ─── KMS ──────────────────────────────────────────────────────────────────────

variable "kms_key_arn" {
  description = "ARN da chave KMS usada para SSE-KMS nos buckets e no Firehose. Deve ser uma chave gerenciada pelo cliente (CMK)."
  type        = string

  validation {
    condition     = can(regex("^arn:aws:kms:", var.kms_key_arn))
    error_message = "kms_key_arn deve ser um ARN válido de chave KMS (arn:aws:kms:...)."
  }
}

# ─── Glue ─────────────────────────────────────────────────────────────────────

variable "glue_database_name" {
  description = "Nome do banco de dados no Glue Catalog para os DecisionTrails."
  type        = string
  default     = "validation_engine_trails"

  validation {
    condition     = can(regex("^[a-z0-9_]+$", var.glue_database_name))
    error_message = "glue_database_name deve conter apenas letras minúsculas, números e underscores."
  }
}

variable "glue_table_name" {
  description = "Nome da tabela no Glue Catalog com o schema do DecisionTrail."
  type        = string
  default     = "decision_trails"

  validation {
    condition     = can(regex("^[a-z0-9_]+$", var.glue_table_name))
    error_message = "glue_table_name deve conter apenas letras minúsculas, números e underscores."
  }
}

# ─── Buffer ───────────────────────────────────────────────────────────────────

variable "buffer_size_mb" {
  description = "Tamanho do buffer do Firehose em MB antes de entregar ao S3 (1-128)."
  type        = number
  default     = 64

  validation {
    condition     = var.buffer_size_mb >= 1 && var.buffer_size_mb <= 128
    error_message = "buffer_size_mb deve estar entre 1 e 128 MB."
  }
}

variable "buffer_interval_seconds" {
  description = "Intervalo máximo do buffer do Firehose em segundos antes de entregar ao S3 (60-900)."
  type        = number
  default     = 300

  validation {
    condition     = var.buffer_interval_seconds >= 60 && var.buffer_interval_seconds <= 900
    error_message = "buffer_interval_seconds deve estar entre 60 e 900 segundos."
  }
}

# ─── IAM ──────────────────────────────────────────────────────────────────────

variable "emitter_principal_arns" {
  description = "Lista de ARNs de IAM principals (roles) que recebem permissão de emissão ao Firehose (Data Plane / runtime)."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for arn in var.emitter_principal_arns : can(regex("^arn:aws:iam:", arn))])
    error_message = "Todos os ARNs em emitter_principal_arns devem ser ARNs IAM válidos (arn:aws:iam:...)."
  }
}

# ─── Lifecycle ────────────────────────────────────────────────────────────────

variable "trail_retention_days" {
  description = "Dias de retenção dos DecisionTrails no bucket principal antes de transição para Glacier IR."
  type        = number
  default     = 365

  validation {
    condition     = var.trail_retention_days >= 30
    error_message = "trail_retention_days deve ser >= 30."
  }
}

variable "error_retention_days" {
  description = "Dias de retenção dos registros de erro no bucket de erros antes de expiração."
  type        = number
  default     = 30

  validation {
    condition     = var.error_retention_days >= 1
    error_message = "error_retention_days deve ser >= 1."
  }
}

# ─── Tags ─────────────────────────────────────────────────────────────────────

variable "tags" {
  description = "Tags aplicadas a todos os recursos do módulo."
  type        = map(string)
  default     = {}
}
