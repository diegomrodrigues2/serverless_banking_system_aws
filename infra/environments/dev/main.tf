# ─────────────────────────────────────────────────────────────────────────────
# Root Module — Ambiente Dev do Double-Entry Ledger
#
# Compõe os módulos:
#   - dynamodb:       tabela single-table com PITR, Streams e TTL
#   - s3-audit:       buckets WORM de auditoria e erros do Firehose
#   - firehose-audit: pipeline Firehose + Glue para auditoria em Parquet
#   - eventbridge:    event bus e rules para TransactionCreated/Reversed
#   - lambda (x4):    Write, Read, Publisher e Audit Transform Lambdas
#
# Pré-requisitos:
#   - Bucket de state S3 criado manualmente (bootstrap)
#   - Autenticação via SSO: `aws sso login --profile dev`
#   - Artifact ZIP em var.lambda_artifact_path
# ─────────────────────────────────────────────────────────────────────────────

locals {
  name_prefix = "${var.project}-${var.environment}"

  common_tags = {
    Environment = var.environment
    Project     = var.project
    ManagedBy   = "terraform"
  }

  # Hash do artifact ZIP para detectar mudanças e forçar redeploy das Lambdas
  lambda_source_hash = filebase64sha256(var.lambda_artifact_path)
}

# ─── DynamoDB ─────────────────────────────────────────────────────────────────

module "dynamodb" {
  source = "../../modules/dynamodb"

  table_name = var.dynamodb_table_name
  tags       = local.common_tags
}

# ─── S3 Audit ─────────────────────────────────────────────────────────────────

module "s3_audit" {
  source = "../../modules/s3-audit"

  audit_bucket_name = var.audit_bucket_name
  error_bucket_name = var.error_bucket_name
  tags              = local.common_tags
}

# ─── Firehose + Glue ──────────────────────────────────────────────────────────

module "firehose_audit" {
  source = "../../modules/firehose-audit"

  stream_name      = var.firehose_stream_name
  audit_bucket_arn = module.s3_audit.audit_bucket_arn
  error_bucket_arn = module.s3_audit.error_bucket_arn
  tags             = local.common_tags
}

# ─── EventBridge ──────────────────────────────────────────────────────────────

module "eventbridge" {
  source = "../../modules/eventbridge"

  event_bus_name = var.event_bus_name
  tags           = local.common_tags
}

# ─── SQS DLQs ────────────────────────────────────────────────────────────────
# DLQ para o Publisher Lambda (falhas na publicação de eventos de negócio)

resource "aws_sqs_queue" "publisher_dlq" {
  name                      = "${local.name_prefix}-publisher-dlq"
  message_retention_seconds = 1209600 # 14 dias

  tags = merge(local.common_tags, { Component = "publisher-dlq" })
}

# DLQ separada para o Audit Transform Lambda (falhas no pipeline de auditoria)
resource "aws_sqs_queue" "audit_dlq" {
  name                      = "${local.name_prefix}-audit-dlq"
  message_retention_seconds = 1209600 # 14 dias

  tags = merge(local.common_tags, { Component = "audit-dlq" })
}

# ─── Lambda: Write ────────────────────────────────────────────────────────────

module "lambda_write" {
  source = "../../modules/lambda"

  function_name    = "${local.name_prefix}-write"
  function_type    = "write"
  handler          = "ledger.api.write_handler.handler"
  runtime          = var.lambda_runtime
  filename         = var.lambda_artifact_path
  source_code_hash = local.lambda_source_hash

  dynamodb_table_arn = module.dynamodb.table_arn

  environment_variables = {
    DYNAMODB_TABLE_NAME = module.dynamodb.table_name
    ENVIRONMENT         = var.environment
    LOG_LEVEL           = "INFO"

    # Validation Engine — variáveis de ambiente para o Data Plane
    VALIDATION_ENGINE_BUNDLE_BUCKET        = module.validation_engine_s3.bundles_bucket_name
    VALIDATION_ENGINE_APPCONFIG_APP_ID     = module.appconfig_validation.application_id
    VALIDATION_ENGINE_APPCONFIG_ENV_ID     = module.appconfig_validation.environment_id
    VALIDATION_ENGINE_APPCONFIG_PROFILE_ID = module.appconfig_validation.configuration_profile_id
    VALIDATION_ENGINE_FIREHOSE_STREAM      = module.firehose_decision_trail.firehose_stream_name
    VALIDATION_ENGINE_KMS_KEY_ARN          = aws_kms_key.validation_engine.arn
  }

  tags = local.common_tags
}

# ─── Validation Engine — IAM Policy Attachments (Write Lambda) ────────────────
#
# Anexa as policies IAM least-privilege dos módulos do Validation Engine à role
# da Write Lambda. Cada policy é gerenciada pelo respectivo módulo e concede
# apenas as permissões mínimas necessárias.
#
# Requisitos: 20.5, 22.7

resource "aws_iam_role_policy_attachment" "write_lambda_validation_s3_reader" {
  role       = module.lambda_write.role_name
  policy_arn = module.validation_engine_s3.reader_policy_arn
}

resource "aws_iam_role_policy_attachment" "write_lambda_validation_appconfig_reader" {
  role       = module.lambda_write.role_name
  policy_arn = module.appconfig_validation.reader_policy_arn
}

resource "aws_iam_role_policy_attachment" "write_lambda_validation_firehose_emitter" {
  role       = module.lambda_write.role_name
  policy_arn = module.firehose_decision_trail.emitter_policy_arn
}

# ─── Lambda: Read ─────────────────────────────────────────────────────────────

module "lambda_read" {
  source = "../../modules/lambda"

  function_name    = "${local.name_prefix}-read"
  function_type    = "read"
  handler          = "ledger.api.read_handler.handler"
  runtime          = var.lambda_runtime
  filename         = var.lambda_artifact_path
  source_code_hash = local.lambda_source_hash

  dynamodb_table_arn = module.dynamodb.table_arn

  environment_variables = {
    DYNAMODB_TABLE_NAME = module.dynamodb.table_name
    ENVIRONMENT         = var.environment
    LOG_LEVEL           = "INFO"
  }

  tags = local.common_tags
}

# ─── Lambda: Publisher ────────────────────────────────────────────────────────

module "lambda_publisher" {
  source = "../../modules/lambda"

  function_name    = "${local.name_prefix}-publisher"
  function_type    = "publisher"
  handler          = "ledger.infrastructure.publisher.handler"
  runtime          = var.lambda_runtime
  filename         = var.lambda_artifact_path
  source_code_hash = local.lambda_source_hash

  dynamodb_stream_arn = module.dynamodb.stream_arn
  event_bus_arn       = module.eventbridge.event_bus_arn
  dlq_arn             = aws_sqs_queue.publisher_dlq.arn

  environment_variables = {
    EVENT_BUS_NAME = module.eventbridge.event_bus_name
    ENVIRONMENT    = var.environment
    LOG_LEVEL      = "INFO"
  }

  tags = local.common_tags
}

# ─── Lambda: Audit Transform ──────────────────────────────────────────────────

module "lambda_audit_transform" {
  source = "../../modules/lambda"

  function_name    = "${local.name_prefix}-audit-transform"
  function_type    = "audit_transform"
  handler          = "ledger.infrastructure.audit_handler.handler"
  runtime          = var.lambda_runtime
  filename         = var.lambda_artifact_path
  source_code_hash = local.lambda_source_hash

  dynamodb_stream_arn = module.dynamodb.stream_arn
  firehose_stream_arn = module.firehose_audit.firehose_stream_arn
  dlq_arn             = aws_sqs_queue.audit_dlq.arn

  # Batch maior e janela de 30s para eficiência do Firehose
  event_source_batch_size      = 100
  event_source_batching_window = 30

  environment_variables = {
    AUDIT_FIREHOSE_STREAM_NAME = module.firehose_audit.firehose_stream_name
    ENVIRONMENT                = var.environment
    LOG_LEVEL                  = "INFO"
  }

  tags = local.common_tags
}

# ─── Validation Engine — KMS CMK ─────────────────────────────────────────────
#
# Chave KMS dedicada ao Validation Engine (bundles, snapshots e bucket de erros).
# Separada da CMK do ledger para isolar blast radius e facilitar auditoria.

resource "aws_kms_key" "validation_engine" {
  description             = "CMK para o Validation Engine — bundles e snapshots"
  deletion_window_in_days = 7 # dev: janela curta para facilitar limpeza
  enable_key_rotation     = true

  tags = merge(local.common_tags, { Module = "validation-engine-s3" })
}

resource "aws_kms_alias" "validation_engine" {
  name          = "alias/${var.project}-validation-engine-${var.environment}"
  target_key_id = aws_kms_key.validation_engine.key_id
}

# ─── Validation Engine — Storage (S3 WORM) ───────────────────────────────────
#
# Compõe o módulo validation-engine-s3 com state no mesmo backend S3 do dev,
# usando prefixo distinto (double-entry-ledger/dev/terraform.tfstate já cobre
# este root module). Requisito 22.5: state separado por componente — aqui
# adotamos a abordagem de prefixo distinto dentro do mesmo root module dev,
# aceitável para dev. Em staging/prod, extrair para root module próprio.

module "validation_engine_s3" {
  source = "../../modules/validation-engine-s3"

  bundles_bucket_name = var.validation_bundles_bucket_name
  error_bucket_name   = var.validation_error_bucket_name
  kms_key_arn         = aws_kms_key.validation_engine.arn

  # Retenção reduzida para dev — facilita limpeza de artefatos de teste
  object_lock_retention_days = 1

  # Write Lambda precisa ler bundles e snapshots para avaliação de policies
  reader_principal_arns = [module.lambda_write.role_arn]
  writer_principal_arns = []

  tags = local.common_tags
}

# ─── Validation Engine — AppConfig ───────────────────────────────────────────
#
# Provisiona o AppConfig para publicação e resolução de PolicyActivationManifests.
# Usa estratégia all-at-once em dev (growth_factor=100, duration=0) para
# facilitar iteração rápida durante desenvolvimento.
#
# Em staging/prod, ajustar growth_factor e deployment_duration_in_minutes
# para rollout gradual (linear ou canary).

module "appconfig_validation" {
  source = "../../modules/appconfig-validation"

  application_name           = var.appconfig_validation_app_name
  environment_name           = var.environment
  configuration_profile_name = "policy-activation-manifests"

  # dev: all-at-once — deployment imediato para facilitar iteração
  deployment_duration_in_minutes = 0
  growth_factor                  = 100
  final_bake_time_in_minutes     = 0

  # Write Lambda precisa resolver PolicyActivationManifests no runtime
  reader_principal_arns    = [module.lambda_write.role_arn]
  publisher_principal_arns = []

  tags = local.common_tags
}

# ─── Validation Engine — Firehose Decision Trail ─────────────────────────────
#
# Pipeline de analytics para DecisionTrails do Validation Engine.
# Converte JSON → Parquet/Snappy e particiona por year/month/day/tenant_id/policy_scope_id.
# Usa a mesma CMK KMS do Validation Engine para consistência de controle de acesso.
#
# Requisitos: 13.5, 21.1, 21.3, 21.4, 21.5, 22.3

module "firehose_decision_trail" {
  source = "../../modules/firehose-decision-trail"

  stream_name       = var.validation_trail_firehose_stream_name
  trail_bucket_name = var.validation_trail_bucket_name
  error_bucket_name = var.validation_trail_error_bucket_name
  kms_key_arn       = aws_kms_key.validation_engine.arn

  # Buffer mínimo exigido pela AWS quando data format conversion (Parquet) está habilitado: 64 MB
  # Ref: BufferingHints.SizeInMBs must be at least 64 when data format conversion is enabled
  buffer_size_mb          = 64
  buffer_interval_seconds = 60

  # Retenção reduzida para dev — facilita limpeza de dados de teste
  trail_retention_days = 30
  error_retention_days = 7

  # Write Lambda precisa emitir DecisionTrails ao Firehose
  emitter_principal_arns = [module.lambda_write.role_arn]

  tags = local.common_tags
}

# ─── Validation Engine — CloudWatch Alarms ───────────────────────────────────
#
# Alarmes mínimos para o runtime do Validation Engine em dev.
# Em dev, alarm_actions é vazio — sem notificações SNS.
# Em staging/prod, adicionar ARN do SNS topic para notificações.
#
# Requisitos: 18.3, 18.5, 22.4

module "cloudwatch_validation_alarms" {
  source = "../../modules/cloudwatch-validation-alarms"

  name_prefix = "${local.name_prefix}-validation"
  namespace   = "ValidationEngine"

  # dev: sem notificações SNS — alarmes visíveis no console CloudWatch
  alarm_actions = []
  ok_actions    = []

  # Thresholds mais permissivos em dev para evitar ruído durante desenvolvimento
  engine_not_ready_threshold  = 5
  refresh_failure_threshold   = 10
  integrity_failure_threshold = 1
  emitter_failure_threshold   = 20

  # Taxa de rejeição anômala: 80% em dev (mais permissivo que prod)
  anomalous_rejections_threshold = 80

  tags = local.common_tags
}

# ─── API Gateway ──────────────────────────────────────────────────────────────

module "api_gateway" {
  source = "../../modules/api-gateway"

  api_name = "${local.name_prefix}-api"

  write_lambda_invoke_arn    = module.lambda_write.invoke_arn
  write_lambda_function_name = module.lambda_write.function_name
  read_lambda_invoke_arn     = module.lambda_read.invoke_arn
  read_lambda_function_name  = module.lambda_read.function_name

  throttling_burst_limit = var.api_throttling_burst_limit
  throttling_rate_limit  = var.api_throttling_rate_limit
  log_retention_days     = var.api_log_retention_days

  tags = local.common_tags
}
