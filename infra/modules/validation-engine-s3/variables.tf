# ─────────────────────────────────────────────────────────────────────────────
# variables.tf — Parâmetros do módulo validation-engine-s3
# ─────────────────────────────────────────────────────────────────────────────

# ─── Nomes dos buckets ────────────────────────────────────────────────────────

variable "bundles_bucket_name" {
  description = "Nome do bucket S3 principal para armazenamento de RuleBundles e ReferenceSnapshots (WORM + SSE-KMS)"
  type        = string

  validation {
    condition     = length(var.bundles_bucket_name) >= 3 && length(var.bundles_bucket_name) <= 63
    error_message = "O nome do bucket deve ter entre 3 e 63 caracteres."
  }
}

variable "error_bucket_name" {
  description = "Nome do bucket S3 dedicado para registros de erro do pipeline de artefatos"
  type        = string

  validation {
    condition     = length(var.error_bucket_name) >= 3 && length(var.error_bucket_name) <= 63
    error_message = "O nome do bucket deve ter entre 3 e 63 caracteres."
  }
}

# ─── KMS ──────────────────────────────────────────────────────────────────────

variable "kms_key_arn" {
  description = "ARN da chave KMS usada para SSE-KMS nos buckets. Deve ser uma chave gerenciada pelo cliente (CMK)."
  type        = string

  validation {
    condition     = can(regex("^arn:aws:kms:", var.kms_key_arn))
    error_message = "kms_key_arn deve ser um ARN válido de chave KMS (arn:aws:kms:...)."
  }
}

# ─── Object Lock ──────────────────────────────────────────────────────────────

variable "object_lock_mode" {
  description = "Modo de Object Lock WORM para o bucket principal: GOVERNANCE ou COMPLIANCE. GOVERNANCE permite que admins com permissão s3:BypassGovernanceRetention removam objetos em casos excepcionais."
  type        = string
  default     = "GOVERNANCE"

  validation {
    condition     = contains(["GOVERNANCE", "COMPLIANCE"], var.object_lock_mode)
    error_message = "object_lock_mode deve ser GOVERNANCE ou COMPLIANCE."
  }
}

variable "object_lock_retention_days" {
  description = "Período de retenção WORM em dias para o bucket principal. Padrão: 2555 (~7 anos) para compliance financeiro."
  type        = number
  default     = 2555

  validation {
    condition     = var.object_lock_retention_days >= 1
    error_message = "object_lock_retention_days deve ser >= 1."
  }
}

# ─── IAM — identidades que recebem permissões ─────────────────────────────────

variable "reader_principal_arns" {
  description = "Lista de ARNs de IAM principals (roles ou users) que recebem permissão de leitura nos prefixos bundles/ e snapshots/ do bucket principal."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for arn in var.reader_principal_arns : can(regex("^arn:aws:iam:", arn))])
    error_message = "Todos os ARNs em reader_principal_arns devem ser ARNs IAM válidos (arn:aws:iam:...)."
  }
}

variable "writer_principal_arns" {
  description = "Lista de ARNs de IAM principals (roles ou users) que recebem permissão de escrita nos prefixos bundles/ e snapshots/ do bucket principal."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for arn in var.writer_principal_arns : can(regex("^arn:aws:iam:", arn))])
    error_message = "Todos os ARNs em writer_principal_arns devem ser ARNs IAM válidos (arn:aws:iam:...)."
  }
}

# ─── Lifecycle ────────────────────────────────────────────────────────────────

variable "transition_to_ia_days" {
  description = "Dias até transição dos artefatos para S3 Standard-IA"
  type        = number
  default     = 180

  validation {
    condition     = var.transition_to_ia_days >= 30
    error_message = "transition_to_ia_days deve ser >= 30 (mínimo exigido pelo S3 para Standard-IA)."
  }
}

variable "transition_to_glacier_days" {
  description = "Dias até transição dos artefatos para S3 Glacier Instant Retrieval"
  type        = number
  default     = 730

  validation {
    condition     = var.transition_to_glacier_days >= 90
    error_message = "transition_to_glacier_days deve ser >= 90."
  }
}

# ─── Tags ─────────────────────────────────────────────────────────────────────

variable "tags" {
  description = "Tags aplicadas a todos os recursos do módulo"
  type        = map(string)
  default     = {}
}
