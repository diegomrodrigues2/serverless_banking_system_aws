# ─────────────────────────────────────────────────────────────────────────────
# outputs.tf — Outputs do módulo appconfig-validation
#
# Expõe apenas o necessário para composição no root module e para
# configuração do ManifestResolver e PolicyPublisher.
# Nenhum segredo ou dado sensível é exposto.
# ─────────────────────────────────────────────────────────────────────────────

# ─── Application ──────────────────────────────────────────────────────────────

output "application_id" {
  description = "ID da AppConfig Application do Validation Engine"
  value       = aws_appconfig_application.validation_engine.id
}

output "application_name" {
  description = "Nome da AppConfig Application do Validation Engine"
  value       = aws_appconfig_application.validation_engine.name
}

# ─── Environment ──────────────────────────────────────────────────────────────

output "environment_id" {
  description = "ID do AppConfig Environment do Validation Engine"
  value       = aws_appconfig_environment.validation_engine.environment_id
}

output "environment_name" {
  description = "Nome do AppConfig Environment do Validation Engine"
  value       = aws_appconfig_environment.validation_engine.name
}

# ─── Configuration Profile ────────────────────────────────────────────────────

output "configuration_profile_id" {
  description = "ID do AppConfig Configuration Profile para PolicyActivationManifests"
  value       = aws_appconfig_configuration_profile.manifests.id
}

output "configuration_profile_name" {
  description = "Nome do AppConfig Configuration Profile para PolicyActivationManifests"
  value       = aws_appconfig_configuration_profile.manifests.name
}

# ─── Deployment Strategy ──────────────────────────────────────────────────────

output "deployment_strategy_id" {
  description = "ID da estratégia de deployment usada. Pode ser a criada pelo módulo ou uma externa."
  value = (
    var.deployment_strategy_name == null
    ? aws_appconfig_deployment_strategy.validation_engine[0].id
    : var.deployment_strategy_name
  )
}

# ─── IAM Policies ─────────────────────────────────────────────────────────────

output "reader_policy_arn" {
  description = "ARN da IAM policy de leitura least-privilege (Data Plane / ManifestResolver). Anexar à role do runtime."
  value       = aws_iam_policy.reader.arn
}

output "publisher_policy_arn" {
  description = "ARN da IAM policy de publicação least-privilege (Control Plane / PolicyPublisher). Anexar à role do Control Plane."
  value       = aws_iam_policy.publisher.arn
}
