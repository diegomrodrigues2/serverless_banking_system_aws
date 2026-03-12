# ─────────────────────────────────────────────────────────────────────────────
# Módulo appconfig-validation — AppConfig para PolicyActivationManifests
#
# Provisiona a infraestrutura AppConfig necessária para o Validation Engine
# publicar e resolver PolicyActivationManifests por escopo de policy.
#
# Recursos criados:
#
#   1. aws_appconfig_application:
#      - Application dedicada ao Validation Engine
#
#   2. aws_appconfig_environment:
#      - Environment por ambiente de execução (dev/staging/prod)
#
#   3. aws_appconfig_configuration_profile:
#      - Profile do tipo "freeform" para os manifestos JSON
#      - Validator de schema JSON para garantir estrutura mínima do manifesto
#
#   4. aws_appconfig_deployment_strategy:
#      - Estratégia por ambiente:
#        * dev:     all-at-once (growth_factor=100, duration=0)
#        * staging: linear 10 min (growth_factor=10, duration=10)
#        * prod:    canary/linear (growth_factor=10, duration=30, bake=5)
#      - Criada apenas quando deployment_strategy_name não é fornecido
#
#   5. IAM Policies (least privilege):
#      - reader:    GetLatestConfiguration + StartConfigurationSession (Data Plane)
#      - publisher: CreateDeployment + GetDeployment (Control Plane)
#
# Estrutura do manifesto (payload do Configuration Profile):
#   {
#     "version": "1",
#     "scopes": {
#       "tenantA:TRANSFER:PIX:*:prod": {
#         "activation_id": "act_2026_03_11_001",
#         "artifact_hash": "sha256:...",
#         "snapshot_version": "snap_2026_03_11_001",
#         "context_schema_version": "1.0",
#         "evaluator_version": "1.2.0"
#       }
#     }
#   }
#
# Requisitos cobertos: 4.1, 22.2, 22.7
# ─────────────────────────────────────────────────────────────────────────────

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# ─── AppConfig Application ────────────────────────────────────────────────────
#
# Application dedicada ao Validation Engine.
# Separada de outras aplicações AppConfig para isolar blast radius e
# facilitar auditoria de mudanças de policy.

resource "aws_appconfig_application" "validation_engine" {
  name        = var.application_name
  description = "Validation Engine — PolicyActivationManifests por escopo de policy"

  tags = merge(var.tags, {
    Module    = "appconfig-validation"
    Component = "application"
  })
}

# ─── AppConfig Environment ────────────────────────────────────────────────────
#
# Environment representa o ambiente de execução do Data Plane.
# Cada ambiente (dev/staging/prod) tem seu próprio environment AppConfig,
# permitindo manifestos distintos por ambiente sem risco de cross-contamination.

resource "aws_appconfig_environment" "validation_engine" {
  name           = var.environment_name
  application_id = aws_appconfig_application.validation_engine.id
  description    = "Ambiente ${var.environment_name} do Validation Engine"

  tags = merge(var.tags, {
    Module    = "appconfig-validation"
    Component = "environment"
  })
}

# ─── AppConfig Configuration Profile ─────────────────────────────────────────
#
# Profile do tipo "freeform" para os PolicyActivationManifests em JSON.
# O validator de schema JSON garante que o payload publicado tenha a
# estrutura mínima esperada pelo ManifestResolver antes do deployment.
#
# Tipo "freeform" é adequado porque o manifesto é JSON livre (não SSM Parameter).
# O location_uri "hosted" indica que o conteúdo é armazenado diretamente no AppConfig.

resource "aws_appconfig_configuration_profile" "manifests" {
  application_id = aws_appconfig_application.validation_engine.id
  name           = var.configuration_profile_name
  description    = "PolicyActivationManifests — mapeamento de escopos para bundles e snapshots ativos"

  # "hosted" armazena o conteúdo diretamente no AppConfig (sem S3 ou SSM externo)
  location_uri = "hosted"

  # Validator de schema JSON — garante estrutura mínima antes do deployment.
  # O schema valida que o payload tem "version" e "scopes" com a estrutura esperada.
  # Isso previne que manifestos malformados sejam ativados no Data Plane.
  validator {
    type = "JSON_SCHEMA"
    content = jsonencode({
      "$schema" = "http://json-schema.org/draft-07/schema#"
      type      = "object"
      required  = ["version", "scopes"]
      properties = {
        version = {
          type = "string"
          enum = ["1"]
        }
        scopes = {
          type = "object"
          additionalProperties = {
            type     = "object"
            required = ["activation_id", "artifact_hash", "snapshot_version", "context_schema_version", "evaluator_version"]
            properties = {
              activation_id          = { type = "string", minLength = 1 }
              artifact_hash          = { type = "string", minLength = 1 }
              snapshot_version       = { type = "string", minLength = 1 }
              context_schema_version = { type = "string", minLength = 1 }
              evaluator_version      = { type = "string", minLength = 1 }
            }
          }
        }
      }
    })
  }

  tags = merge(var.tags, {
    Module    = "appconfig-validation"
    Component = "configuration-profile"
  })
}

# ─── Deployment Strategy ──────────────────────────────────────────────────────
#
# Estratégia de deployment criada pelo módulo quando deployment_strategy_name
# não é fornecido externamente.
#
# Estratégia por ambiente (Requisito 22.2 — rollout controlado):
#   dev:     all-at-once  — growth_factor=100, duration=0, bake=0
#   staging: linear       — growth_factor=10,  duration=10, bake=2
#   prod:    canary/linear — growth_factor=10,  duration=30, bake=5
#
# O root module pode sobrescrever os parâmetros via variáveis para ajuste fino.

resource "aws_appconfig_deployment_strategy" "validation_engine" {
  # Cria a estratégia apenas quando o nome não é fornecido externamente.
  # count = 0 quando deployment_strategy_name é fornecido (usa estratégia existente).
  count = var.deployment_strategy_name == null ? 1 : 0

  name        = "${var.application_name}-${var.environment_name}-strategy"
  description = "Estratégia de deployment do Validation Engine para ${var.environment_name}"

  # Réplica para todos os clientes AppConfig (NONE = sem réplica adicional)
  replicate_to = "NONE"

  deployment_duration_in_minutes = var.deployment_duration_in_minutes
  growth_factor                  = var.growth_factor
  final_bake_time_in_minutes     = var.final_bake_time_in_minutes

  # LINEAR é o tipo de crescimento padrão — adequado para dev e staging.
  # Para prod com canary real, usar EXPONENTIAL com growth_factor menor.
  growth_type = "LINEAR"

  tags = merge(var.tags, {
    Module    = "appconfig-validation"
    Component = "deployment-strategy"
  })
}

# ─── IAM Policy — Leitura (Data Plane / Runtime) ──────────────────────────────
#
# Permissões mínimas para o runtime do Validation Engine resolver manifestos.
# O ManifestResolver usa StartConfigurationSession + GetLatestConfiguration
# para obter o manifesto ativo por escopo.
#
# Least privilege: acesso restrito à application e environment específicos.

resource "aws_iam_policy" "reader" {
  name        = "${var.application_name}-${var.environment_name}-reader"
  description = "Leitura least-privilege de PolicyActivationManifests do AppConfig (Data Plane)"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # Iniciar sessão de configuração — necessário antes de GetLatestConfiguration
      {
        Sid    = "StartConfigurationSession"
        Effect = "Allow"
        Action = [
          "appconfig:StartConfigurationSession",
          "appconfig:GetLatestConfiguration"
        ]
        Resource = "${aws_appconfig_environment.validation_engine.arn}/configuration/${aws_appconfig_configuration_profile.manifests.id}"
      },
      # GetConfiguration (API legada) — mantido para compatibilidade com SDKs antigos
      {
        Sid    = "GetConfiguration"
        Effect = "Allow"
        Action = [
          "appconfig:GetConfiguration"
        ]
        Resource = "${aws_appconfig_environment.validation_engine.arn}/configuration/${aws_appconfig_configuration_profile.manifests.id}"
      }
    ]
  })

  tags = merge(var.tags, { Module = "appconfig-validation" })
}

# ─── IAM Policy — Publicação (Control Plane) ──────────────────────────────────
#
# Permissões mínimas para o Control Plane publicar novos manifestos.
# O PolicyPublisher usa CreateHostedConfigurationVersion + StartDeployment
# para publicar e ativar um novo manifesto.
#
# Least privilege: acesso restrito à application e profile específicos.

resource "aws_iam_policy" "publisher" {
  name        = "${var.application_name}-${var.environment_name}-publisher"
  description = "Publicação least-privilege de PolicyActivationManifests no AppConfig (Control Plane)"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # Criar nova versão do manifesto no AppConfig Hosted Configuration
      {
        Sid    = "CreateHostedConfigurationVersion"
        Effect = "Allow"
        Action = [
          "appconfig:CreateHostedConfigurationVersion"
        ]
        Resource = "arn:aws:appconfig:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:application/${aws_appconfig_application.validation_engine.id}/configurationprofile/${aws_appconfig_configuration_profile.manifests.id}"
      },
      # Iniciar deployment do manifesto publicado
      {
        Sid    = "StartDeployment"
        Effect = "Allow"
        Action = [
          "appconfig:StartDeployment",
          "appconfig:GetDeployment",
          "appconfig:StopDeployment"
        ]
        Resource = "${aws_appconfig_environment.validation_engine.arn}/deployment/*"
      },
      # Listar deployments para verificar status e histórico (auditoria e rollback)
      {
        Sid    = "ListDeployments"
        Effect = "Allow"
        Action = [
          "appconfig:ListDeployments",
          "appconfig:GetDeploymentStrategy"
        ]
        Resource = [
          "arn:aws:appconfig:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:application/${aws_appconfig_application.validation_engine.id}",
          aws_appconfig_environment.validation_engine.arn
        ]
      }
    ]
  })

  tags = merge(var.tags, { Module = "appconfig-validation" })
}

# ─── Anexar policy de leitura aos principals declarados ───────────────────────

resource "aws_iam_role_policy_attachment" "reader" {
  for_each = toset(var.reader_principal_arns)

  # Extrai o nome da role do ARN (último segmento após "/")
  role       = element(split("/", each.value), length(split("/", each.value)) - 1)
  policy_arn = aws_iam_policy.reader.arn
}

# ─── Anexar policy de publicação aos principals declarados ────────────────────

resource "aws_iam_role_policy_attachment" "publisher" {
  for_each = toset(var.publisher_principal_arns)

  role       = element(split("/", each.value), length(split("/", each.value)) - 1)
  policy_arn = aws_iam_policy.publisher.arn
}
