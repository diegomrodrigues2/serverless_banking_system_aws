variable "table_name" {
  description = "Nome da tabela DynamoDB (single-table design)"
  type        = string

  validation {
    condition     = length(var.table_name) >= 3 && length(var.table_name) <= 255
    error_message = "O nome da tabela deve ter entre 3 e 255 caracteres."
  }
}

variable "billing_mode" {
  description = "Modo de cobrança da tabela: PAY_PER_REQUEST ou PROVISIONED"
  type        = string
  default     = "PAY_PER_REQUEST"

  validation {
    condition     = contains(["PAY_PER_REQUEST", "PROVISIONED"], var.billing_mode)
    error_message = "billing_mode deve ser PAY_PER_REQUEST ou PROVISIONED."
  }
}

variable "ttl_attribute" {
  description = "Nome do atributo TTL para expiração automática de OutboxEvents"
  type        = string
  default     = "expires_at"
}

variable "stream_view_type" {
  description = "Tipo de view do DynamoDB Stream"
  type        = string
  default     = "NEW_IMAGE"

  validation {
    condition     = contains(["KEYS_ONLY", "NEW_IMAGE", "OLD_IMAGE", "NEW_AND_OLD_IMAGES"], var.stream_view_type)
    error_message = "stream_view_type deve ser KEYS_ONLY, NEW_IMAGE, OLD_IMAGE ou NEW_AND_OLD_IMAGES."
  }
}

variable "tags" {
  description = "Tags aplicadas a todos os recursos do módulo"
  type        = map(string)
  default     = {}
}
