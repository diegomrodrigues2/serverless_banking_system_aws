variable "event_bus_name" {
  description = "Nome do Event Bus do EventBridge para eventos do ledger"
  type        = string

  validation {
    condition     = length(var.event_bus_name) >= 1 && length(var.event_bus_name) <= 256
    error_message = "O nome do event bus deve ter entre 1 e 256 caracteres."
  }
}

variable "transaction_created_target_arn" {
  description = "ARN do target para eventos TransactionCreated (ex: SQS, Lambda, SNS)"
  type        = string
  default     = ""
}

variable "transaction_reversed_target_arn" {
  description = "ARN do target para eventos TransactionReversed (ex: SQS, Lambda, SNS)"
  type        = string
  default     = ""
}

variable "tags" {
  description = "Tags aplicadas a todos os recursos do módulo"
  type        = map(string)
  default     = {}
}
