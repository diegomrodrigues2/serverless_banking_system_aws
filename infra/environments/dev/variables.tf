variable "aws_region" {
  description = "Região AWS onde os recursos serão provisionados"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Nome do ambiente (dev, staging, prod)"
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment deve ser dev, staging ou prod."
  }
}

variable "project" {
  description = "Nome do projeto"
  type        = string
  default     = "double-entry-ledger"
}

# ─── DynamoDB ─────────────────────────────────────────────────────────────────

variable "dynamodb_table_name" {
  description = "Nome da tabela DynamoDB"
  type        = string
  default     = "ledger-dev"
}

# ─── S3 ───────────────────────────────────────────────────────────────────────

variable "audit_bucket_name" {
  description = "Nome do bucket S3 de auditoria WORM"
  type        = string
  default     = "ledger-audit-worm-dev"
}

variable "error_bucket_name" {
  description = "Nome do bucket S3 de erros do Firehose"
  type        = string
  default     = "ledger-firehose-errors-dev"
}

# ─── Firehose ─────────────────────────────────────────────────────────────────

variable "firehose_stream_name" {
  description = "Nome do Kinesis Data Firehose delivery stream"
  type        = string
  default     = "ledger-audit-dev"
}

# ─── EventBridge ──────────────────────────────────────────────────────────────

variable "event_bus_name" {
  description = "Nome do Event Bus do EventBridge"
  type        = string
  default     = "ledger-events-dev"
}

# ─── Lambda ───────────────────────────────────────────────────────────────────

variable "lambda_artifact_path" {
  description = "Caminho para o arquivo ZIP com o código das Lambdas"
  type        = string
  default     = "../../../dist/ledger.zip"
}

variable "lambda_runtime" {
  description = "Runtime das funções Lambda"
  type        = string
  default     = "python3.11"
}

# ─── API Gateway ──────────────────────────────────────────────────────────────

variable "api_throttling_burst_limit" {
  description = "Limite de burst do API Gateway (requisições simultâneas)"
  type        = number
  default     = 100
}

variable "api_throttling_rate_limit" {
  description = "Limite de rate do API Gateway (requisições por segundo)"
  type        = number
  default     = 50
}

variable "api_log_retention_days" {
  description = "Retenção dos logs de acesso do API Gateway em dias"
  type        = number
  default     = 30
}

# ─── Validation Engine — Storage ──────────────────────────────────────────────

variable "validation_bundles_bucket_name" {
  description = "Nome do bucket S3 principal do Validation Engine (RuleBundles e ReferenceSnapshots, WORM + SSE-KMS)"
  type        = string
  default     = "ledger-validation-bundles-dev"

  validation {
    condition     = length(var.validation_bundles_bucket_name) >= 3 && length(var.validation_bundles_bucket_name) <= 63
    error_message = "O nome do bucket deve ter entre 3 e 63 caracteres."
  }
}

variable "validation_error_bucket_name" {
  description = "Nome do bucket S3 de erros do pipeline de artefatos do Validation Engine"
  type        = string
  default     = "ledger-validation-errors-dev"

  validation {
    condition     = length(var.validation_error_bucket_name) >= 3 && length(var.validation_error_bucket_name) <= 63
    error_message = "O nome do bucket deve ter entre 3 e 63 caracteres."
  }
}

# ─── Validation Engine — AppConfig ────────────────────────────────────────────

variable "appconfig_validation_app_name" {
  description = "Nome da AppConfig Application para o Validation Engine (PolicyActivationManifests)"
  type        = string
  default     = "ledger-validation-engine-dev"

  validation {
    condition     = length(var.appconfig_validation_app_name) >= 1 && length(var.appconfig_validation_app_name) <= 64
    error_message = "appconfig_validation_app_name deve ter entre 1 e 64 caracteres."
  }
}

# ─── Validation Engine — Firehose Decision Trail ───────────────────────────────

variable "validation_trail_firehose_stream_name" {
  description = "Nome do Kinesis Data Firehose delivery stream para DecisionTrails do Validation Engine"
  type        = string
  default     = "ledger-validation-decision-trail-dev"

  validation {
    condition     = length(var.validation_trail_firehose_stream_name) >= 1 && length(var.validation_trail_firehose_stream_name) <= 64
    error_message = "validation_trail_firehose_stream_name deve ter entre 1 e 64 caracteres."
  }
}

variable "validation_trail_bucket_name" {
  description = "Nome do bucket S3 de destino para os DecisionTrails em Parquet"
  type        = string
  default     = "ledger-validation-trails-dev"

  validation {
    condition     = length(var.validation_trail_bucket_name) >= 3 && length(var.validation_trail_bucket_name) <= 63
    error_message = "validation_trail_bucket_name deve ter entre 3 e 63 caracteres."
  }
}

variable "validation_trail_error_bucket_name" {
  description = "Nome do bucket S3 de erros do pipeline Firehose de DecisionTrails"
  type        = string
  default     = "ledger-validation-trail-errors-dev"

  validation {
    condition     = length(var.validation_trail_error_bucket_name) >= 3 && length(var.validation_trail_error_bucket_name) <= 63
    error_message = "validation_trail_error_bucket_name deve ter entre 3 e 63 caracteres."
  }
}
