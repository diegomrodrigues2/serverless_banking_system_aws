# ─────────────────────────────────────────────────────────────────────────────
# variables.tf — Parâmetros do módulo appconfig-validation
#
# Parâmetros para provisionamento do AppConfig Application, Environment,
# Configuration Profile e estratégia de deployment para o Validation Engine.
#
# Requisitos cobertos: 4.1, 22.2, 22.7
# ─────────────────────────────────────────────────────────────────────────────

# ─── Identificação ────────────────────────────────────────────────────────────

variable "application_name" {
  description = "Nome da AppConfig Application para o Validation Engine. Deve ser único na conta AWS."
  type        = string

  validation {
    condition     = length(var.application_name) >= 1 && length(var.application_name) <= 64
    error_message = "application_name deve ter entre 1 e 64 caracteres."
  }
}

variable "environment_name" {
  description = "Nome do AppConfig Environment (ex: dev, staging, prod). Representa o ambiente de execução do Data Plane."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment_name)
    error_message = "environment_name deve ser dev, staging ou prod."
  }
}

variable "configuration_profile_name" {
  description = "Nome do AppConfig Configuration Profile para os PolicyActivationManifests."
  type        = string
  default     = "policy-activation-manifests"

  validation {
    condition     = length(var.configuration_profile_name) >= 1 && length(var.configuration_profile_name) <= 64
    error_message = "configuration_profile_name deve ter entre 1 e 64 caracteres."
  }
}

# ─── Deployment Strategy ──────────────────────────────────────────────────────

variable "deployment_strategy_name" {
  description = "Nome da estratégia de deployment do AppConfig. Controla velocidade e rollout dos manifestos."
  type        = string
  default     = null

  # Quando null, o módulo cria uma estratégia própria baseada no ambiente.
  # Quando fornecido, usa a estratégia existente com este nome.
}

variable "deployment_duration_in_minutes" {
  description = "Duração total do deployment em minutos. Usado quando o módulo cria a estratégia própria."
  type        = number
  default     = 0

  validation {
    condition     = var.deployment_duration_in_minutes >= 0 && var.deployment_duration_in_minutes <= 1440
    error_message = "deployment_duration_in_minutes deve estar entre 0 e 1440 (24h)."
  }
}

variable "growth_factor" {
  description = "Percentual de crescimento por intervalo no deployment. 100 = all-at-once (dev). Menor = rollout gradual (prod)."
  type        = number
  default     = 100

  validation {
    condition     = var.growth_factor >= 1 && var.growth_factor <= 100
    error_message = "growth_factor deve estar entre 1 e 100."
  }
}

variable "final_bake_time_in_minutes" {
  description = "Tempo de bake final em minutos após o deployment completar. Permite observar métricas antes de confirmar."
  type        = number
  default     = 0

  validation {
    condition     = var.final_bake_time_in_minutes >= 0 && var.final_bake_time_in_minutes <= 1440
    error_message = "final_bake_time_in_minutes deve estar entre 0 e 1440."
  }
}

# ─── IAM ──────────────────────────────────────────────────────────────────────

variable "reader_principal_arns" {
  description = "Lista de ARNs de IAM principals (roles) que recebem permissão de leitura do AppConfig (Data Plane / runtime)."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for arn in var.reader_principal_arns : can(regex("^arn:aws:iam:", arn))])
    error_message = "Todos os ARNs em reader_principal_arns devem ser ARNs IAM válidos (arn:aws:iam:...)."
  }
}

variable "publisher_principal_arns" {
  description = "Lista de ARNs de IAM principals (roles) que recebem permissão de publicação de manifestos (Control Plane)."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for arn in var.publisher_principal_arns : can(regex("^arn:aws:iam:", arn))])
    error_message = "Todos os ARNs em publisher_principal_arns devem ser ARNs IAM válidos (arn:aws:iam:...)."
  }
}

# ─── Tags ─────────────────────────────────────────────────────────────────────

variable "tags" {
  description = "Tags aplicadas a todos os recursos do módulo."
  type        = map(string)
  default     = {}
}
