variable "function_name" {
  description = "Nome da função Lambda"
  type        = string

  validation {
    condition     = length(var.function_name) >= 1 && length(var.function_name) <= 64
    error_message = "O nome da função deve ter entre 1 e 64 caracteres."
  }
}

variable "function_type" {
  description = "Tipo da função: write, read, publisher ou audit_transform"
  type        = string

  validation {
    condition     = contains(["write", "read", "publisher", "audit_transform"], var.function_type)
    error_message = "function_type deve ser write, read, publisher ou audit_transform."
  }
}

variable "handler" {
  description = "Handler da função Lambda (ex: ledger.api.write_handler.handler)"
  type        = string
}

variable "runtime" {
  description = "Runtime da função Lambda"
  type        = string
  default     = "python3.11"
}

variable "filename" {
  description = "Caminho para o arquivo ZIP do código da Lambda"
  type        = string
}

variable "source_code_hash" {
  description = "Hash do arquivo ZIP para detectar mudanças"
  type        = string
}

variable "memory_size" {
  description = "Memória alocada para a Lambda em MB"
  type        = number
  default     = 256

  validation {
    condition     = var.memory_size >= 128 && var.memory_size <= 10240
    error_message = "memory_size deve estar entre 128 e 10240 MB."
  }
}

variable "timeout" {
  description = "Timeout da Lambda em segundos"
  type        = number
  default     = 30

  validation {
    condition     = var.timeout >= 1 && var.timeout <= 900
    error_message = "timeout deve estar entre 1 e 900 segundos."
  }
}

variable "environment_variables" {
  description = "Variáveis de ambiente da Lambda"
  type        = map(string)
  default     = {}
}

# ─── Configurações do DynamoDB (para write e read) ───────────────────────────

variable "dynamodb_table_arn" {
  description = "ARN da tabela DynamoDB (necessário para write e read)"
  type        = string
  default     = ""
}

variable "dynamodb_stream_arn" {
  description = "ARN do DynamoDB Stream (necessário para publisher e audit_transform)"
  type        = string
  default     = ""
}

# ─── Configurações do EventBridge (para publisher) ───────────────────────────

variable "event_bus_arn" {
  description = "ARN do Event Bus do EventBridge (necessário para publisher)"
  type        = string
  default     = ""
}

# ─── Configurações do Firehose (para audit_transform) ────────────────────────

variable "firehose_stream_arn" {
  description = "ARN do Kinesis Data Firehose (necessário para audit_transform)"
  type        = string
  default     = ""
}

# ─── Configurações de DLQ ────────────────────────────────────────────────────

variable "dlq_arn" {
  description = "ARN da SQS DLQ para mensagens com falha"
  type        = string
  default     = ""
}

# ─── Configurações do Event Source Mapping ───────────────────────────────────

variable "event_source_batch_size" {
  description = "Tamanho do batch para Event Source Mapping"
  type        = number
  default     = 100

  validation {
    condition     = var.event_source_batch_size >= 1 && var.event_source_batch_size <= 10000
    error_message = "event_source_batch_size deve estar entre 1 e 10000."
  }
}

variable "event_source_batching_window" {
  description = "Janela de batching em segundos para Event Source Mapping"
  type        = number
  default     = 30

  validation {
    condition     = var.event_source_batching_window >= 0 && var.event_source_batching_window <= 300
    error_message = "event_source_batching_window deve estar entre 0 e 300 segundos."
  }
}

variable "event_source_starting_position" {
  description = "Posição inicial de leitura do stream"
  type        = string
  default     = "LATEST"

  validation {
    condition     = contains(["TRIM_HORIZON", "LATEST", "AT_TIMESTAMP"], var.event_source_starting_position)
    error_message = "event_source_starting_position deve ser TRIM_HORIZON, LATEST ou AT_TIMESTAMP."
  }
}

variable "tags" {
  description = "Tags aplicadas a todos os recursos do módulo"
  type        = map(string)
  default     = {}
}
