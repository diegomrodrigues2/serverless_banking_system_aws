variable "audit_bucket_name" {
  description = "Nome do bucket S3 de destino para auditoria WORM"
  type        = string

  validation {
    condition     = length(var.audit_bucket_name) >= 3 && length(var.audit_bucket_name) <= 63
    error_message = "O nome do bucket deve ter entre 3 e 63 caracteres."
  }
}

variable "error_bucket_name" {
  description = "Nome do bucket S3 para registros com falha na conversão Parquet do Firehose"
  type        = string

  validation {
    condition     = length(var.error_bucket_name) >= 3 && length(var.error_bucket_name) <= 63
    error_message = "O nome do bucket deve ter entre 3 e 63 caracteres."
  }
}

variable "object_lock_mode" {
  description = "Modo de Object Lock WORM: GOVERNANCE ou COMPLIANCE"
  type        = string
  default     = "GOVERNANCE"

  validation {
    condition     = contains(["GOVERNANCE", "COMPLIANCE"], var.object_lock_mode)
    error_message = "object_lock_mode deve ser GOVERNANCE ou COMPLIANCE."
  }
}

variable "object_lock_retention_days" {
  description = "Período de retenção WORM em dias"
  type        = number
  default     = 2555 # ~7 anos — prazo comum para compliance financeiro

  validation {
    condition     = var.object_lock_retention_days >= 1
    error_message = "object_lock_retention_days deve ser >= 1."
  }
}

variable "transition_to_ia_days" {
  description = "Dias até transição para S3 Standard-IA"
  type        = number
  default     = 90
}

variable "transition_to_glacier_days" {
  description = "Dias até transição para S3 Glacier Instant Retrieval"
  type        = number
  default     = 365
}

variable "tags" {
  description = "Tags aplicadas a todos os recursos do módulo"
  type        = map(string)
  default     = {}
}
