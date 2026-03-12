# ─────────────────────────────────────────────────────────────────────────────
# variables.tf — Parâmetros do módulo cloudwatch-validation-alarms
#
# Parâmetros para provisionamento dos alarmes CloudWatch do Validation Engine.
#
# Requisitos cobertos: 18.3, 18.5, 22.4, 22.7
# ─────────────────────────────────────────────────────────────────────────────

# ─── Identificação ────────────────────────────────────────────────────────────

variable "name_prefix" {
  description = "Prefixo para nomear todos os alarmes. Ex: 'ledger-validation-dev'."
  type        = string

  validation {
    condition     = length(var.name_prefix) >= 1 && length(var.name_prefix) <= 64
    error_message = "name_prefix deve ter entre 1 e 64 caracteres."
  }
}

variable "namespace" {
  description = "Namespace CloudWatch onde as métricas do Validation Engine são publicadas."
  type        = string
  default     = "ValidationEngine"

  validation {
    condition     = length(var.namespace) >= 1 && length(var.namespace) <= 256
    error_message = "namespace deve ter entre 1 e 256 caracteres."
  }
}

# ─── SNS (notificações) ───────────────────────────────────────────────────────

variable "alarm_actions" {
  description = "Lista de ARNs de ações a executar quando um alarme dispara (ex: SNS topic ARN). Pode ser vazio em dev."
  type        = list(string)
  default     = []
}

variable "ok_actions" {
  description = "Lista de ARNs de ações a executar quando um alarme retorna ao estado OK."
  type        = list(string)
  default     = []
}

# ─── Thresholds — policy_engine_not_ready ─────────────────────────────────────

variable "engine_not_ready_threshold" {
  description = "Número de erros PolicyEngineNotReady em evaluation_periods para disparar o alarme."
  type        = number
  default     = 1

  validation {
    condition     = var.engine_not_ready_threshold >= 1
    error_message = "engine_not_ready_threshold deve ser >= 1."
  }
}

variable "engine_not_ready_evaluation_periods" {
  description = "Número de períodos de avaliação para o alarme policy_engine_not_ready."
  type        = number
  default     = 1

  validation {
    condition     = var.engine_not_ready_evaluation_periods >= 1
    error_message = "engine_not_ready_evaluation_periods deve ser >= 1."
  }
}

variable "engine_not_ready_period_seconds" {
  description = "Duração de cada período de avaliação em segundos para o alarme policy_engine_not_ready."
  type        = number
  default     = 60

  validation {
    condition     = var.engine_not_ready_period_seconds >= 10
    error_message = "engine_not_ready_period_seconds deve ser >= 10."
  }
}

# ─── Thresholds — refresh_failure ─────────────────────────────────────────────

variable "refresh_failure_threshold" {
  description = "Número de falhas de refresh de policy em evaluation_periods para disparar o alarme."
  type        = number
  default     = 3

  validation {
    condition     = var.refresh_failure_threshold >= 1
    error_message = "refresh_failure_threshold deve ser >= 1."
  }
}

variable "refresh_failure_evaluation_periods" {
  description = "Número de períodos de avaliação para o alarme refresh_failure."
  type        = number
  default     = 3

  validation {
    condition     = var.refresh_failure_evaluation_periods >= 1
    error_message = "refresh_failure_evaluation_periods deve ser >= 1."
  }
}

variable "refresh_failure_period_seconds" {
  description = "Duração de cada período de avaliação em segundos para o alarme refresh_failure."
  type        = number
  default     = 60

  validation {
    condition     = var.refresh_failure_period_seconds >= 10
    error_message = "refresh_failure_period_seconds deve ser >= 10."
  }
}

# ─── Thresholds — integrity_failure ───────────────────────────────────────────

variable "integrity_failure_threshold" {
  description = "Número de falhas de integridade de bundle/snapshot para disparar o alarme."
  type        = number
  default     = 1

  validation {
    condition     = var.integrity_failure_threshold >= 1
    error_message = "integrity_failure_threshold deve ser >= 1."
  }
}

variable "integrity_failure_evaluation_periods" {
  description = "Número de períodos de avaliação para o alarme integrity_failure."
  type        = number
  default     = 1

  validation {
    condition     = var.integrity_failure_evaluation_periods >= 1
    error_message = "integrity_failure_evaluation_periods deve ser >= 1."
  }
}

variable "integrity_failure_period_seconds" {
  description = "Duração de cada período de avaliação em segundos para o alarme integrity_failure."
  type        = number
  default     = 60

  validation {
    condition     = var.integrity_failure_period_seconds >= 10
    error_message = "integrity_failure_period_seconds deve ser >= 10."
  }
}

# ─── Thresholds — emitter_failure ─────────────────────────────────────────────

variable "emitter_failure_threshold" {
  description = "Número de falhas de emissão de DecisionTrail para disparar o alarme."
  type        = number
  default     = 10

  validation {
    condition     = var.emitter_failure_threshold >= 1
    error_message = "emitter_failure_threshold deve ser >= 1."
  }
}

variable "emitter_failure_evaluation_periods" {
  description = "Número de períodos de avaliação para o alarme emitter_failure."
  type        = number
  default     = 3

  validation {
    condition     = var.emitter_failure_evaluation_periods >= 1
    error_message = "emitter_failure_evaluation_periods deve ser >= 1."
  }
}

variable "emitter_failure_period_seconds" {
  description = "Duração de cada período de avaliação em segundos para o alarme emitter_failure."
  type        = number
  default     = 300

  validation {
    condition     = var.emitter_failure_period_seconds >= 10
    error_message = "emitter_failure_period_seconds deve ser >= 10."
  }
}

# ─── Thresholds — anomalous_rejections ────────────────────────────────────────

variable "anomalous_rejections_threshold" {
  description = "Taxa de rejeições POLICY_REJECTED (%) acima da qual o alarme dispara. Ex: 50 = 50% de rejeições."
  type        = number
  default     = 50

  validation {
    condition     = var.anomalous_rejections_threshold >= 1 && var.anomalous_rejections_threshold <= 100
    error_message = "anomalous_rejections_threshold deve estar entre 1 e 100."
  }
}

variable "anomalous_rejections_evaluation_periods" {
  description = "Número de períodos de avaliação para o alarme anomalous_rejections."
  type        = number
  default     = 5

  validation {
    condition     = var.anomalous_rejections_evaluation_periods >= 1
    error_message = "anomalous_rejections_evaluation_periods deve ser >= 1."
  }
}

variable "anomalous_rejections_period_seconds" {
  description = "Duração de cada período de avaliação em segundos para o alarme anomalous_rejections."
  type        = number
  default     = 60

  validation {
    condition     = var.anomalous_rejections_period_seconds >= 10
    error_message = "anomalous_rejections_period_seconds deve ser >= 10."
  }
}

# ─── Tags ─────────────────────────────────────────────────────────────────────

variable "tags" {
  description = "Tags aplicadas a todos os recursos do módulo."
  type        = map(string)
  default     = {}
}
