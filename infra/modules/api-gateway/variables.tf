variable "api_name" {
  description = "Nome do HTTP API Gateway"
  type        = string

  validation {
    condition     = length(var.api_name) >= 1 && length(var.api_name) <= 128
    error_message = "api_name deve ter entre 1 e 128 caracteres."
  }
}

# ─── ARNs e nomes das Lambdas ─────────────────────────────────────────────────

variable "write_lambda_invoke_arn" {
  description = "Invoke ARN da Write Lambda (usado na integração do API Gateway)"
  type        = string
}

variable "write_lambda_function_name" {
  description = "Nome da Write Lambda (usado no aws_lambda_permission)"
  type        = string
}

variable "read_lambda_invoke_arn" {
  description = "Invoke ARN da Read Lambda (usado na integração do API Gateway)"
  type        = string
}

variable "read_lambda_function_name" {
  description = "Nome da Read Lambda (usado no aws_lambda_permission)"
  type        = string
}

# ─── Throttling ───────────────────────────────────────────────────────────────

variable "throttling_burst_limit" {
  description = "Limite de burst de requisições por segundo"
  type        = number
  default     = 100

  validation {
    condition     = var.throttling_burst_limit >= 0
    error_message = "throttling_burst_limit deve ser >= 0."
  }
}

variable "throttling_rate_limit" {
  description = "Limite de requisições por segundo (rate)"
  type        = number
  default     = 50

  validation {
    condition     = var.throttling_rate_limit >= 0
    error_message = "throttling_rate_limit deve ser >= 0."
  }
}

# ─── Logs ─────────────────────────────────────────────────────────────────────

variable "log_retention_days" {
  description = "Retenção dos logs de acesso do API Gateway em dias"
  type        = number
  default     = 30

  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1827, 3653], var.log_retention_days)
    error_message = "log_retention_days deve ser um valor válido do CloudWatch Logs."
  }
}

# ─── CORS ─────────────────────────────────────────────────────────────────────

variable "cors_allow_origins" {
  description = "Lista de origens permitidas para CORS"
  type        = list(string)
  default     = ["*"]
}

variable "tags" {
  description = "Tags aplicadas a todos os recursos do módulo"
  type        = map(string)
  default     = {}
}
