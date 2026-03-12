output "dynamodb_table_name" {
  description = "Nome da tabela DynamoDB"
  value       = module.dynamodb.table_name
}

output "dynamodb_table_arn" {
  description = "ARN da tabela DynamoDB"
  value       = module.dynamodb.table_arn
}

output "dynamodb_stream_arn" {
  description = "ARN do DynamoDB Stream"
  value       = module.dynamodb.stream_arn
}

output "audit_bucket_name" {
  description = "Nome do bucket S3 de auditoria WORM"
  value       = module.s3_audit.audit_bucket_name
}

output "firehose_stream_name" {
  description = "Nome do Kinesis Data Firehose delivery stream"
  value       = module.firehose_audit.firehose_stream_name
}

output "event_bus_arn" {
  description = "ARN do Event Bus do EventBridge"
  value       = module.eventbridge.event_bus_arn
}

output "lambda_write_arn" {
  description = "ARN da Write Lambda"
  value       = module.lambda_write.function_arn
}

output "lambda_read_arn" {
  description = "ARN da Read Lambda"
  value       = module.lambda_read.function_arn
}

output "lambda_publisher_arn" {
  description = "ARN da Publisher Lambda"
  value       = module.lambda_publisher.function_arn
}

output "lambda_audit_transform_arn" {
  description = "ARN da Audit Transform Lambda"
  value       = module.lambda_audit_transform.function_arn
}

output "api_endpoint" {
  description = "URL base do API Gateway — use como base_url no Postman"
  value       = module.api_gateway.api_endpoint
}

# ─── Validation Engine — Storage ──────────────────────────────────────────────

output "validation_bundles_bucket_name" {
  description = "Nome do bucket S3 principal do Validation Engine (RuleBundles e ReferenceSnapshots)"
  value       = module.validation_engine_s3.bundles_bucket_name
}

output "validation_bundles_bucket_arn" {
  description = "ARN do bucket S3 principal do Validation Engine"
  value       = module.validation_engine_s3.bundles_bucket_arn
}

output "validation_error_bucket_name" {
  description = "Nome do bucket S3 de erros do Validation Engine"
  value       = module.validation_engine_s3.error_bucket_name
}

output "validation_reader_policy_arn" {
  description = "ARN da IAM policy de leitura do Validation Engine — anexar à role do Data Plane quando criada"
  value       = module.validation_engine_s3.reader_policy_arn
}

output "validation_writer_policy_arn" {
  description = "ARN da IAM policy de escrita do Validation Engine — anexar à role do Control Plane quando criada"
  value       = module.validation_engine_s3.writer_policy_arn
}

# ─── Validation Engine — AppConfig ────────────────────────────────────────────

output "appconfig_validation_application_id" {
  description = "ID da AppConfig Application do Validation Engine"
  value       = module.appconfig_validation.application_id
}

output "appconfig_validation_environment_id" {
  description = "ID do AppConfig Environment do Validation Engine"
  value       = module.appconfig_validation.environment_id
}

output "appconfig_validation_configuration_profile_id" {
  description = "ID do AppConfig Configuration Profile para PolicyActivationManifests"
  value       = module.appconfig_validation.configuration_profile_id
}

output "appconfig_validation_reader_policy_arn" {
  description = "ARN da IAM policy de leitura do AppConfig (Data Plane) — anexar à role do runtime quando criada"
  value       = module.appconfig_validation.reader_policy_arn
}

output "appconfig_validation_publisher_policy_arn" {
  description = "ARN da IAM policy de publicação do AppConfig (Control Plane) — anexar à role do Control Plane quando criada"
  value       = module.appconfig_validation.publisher_policy_arn
}

# ─── Validation Engine — Firehose Decision Trail ──────────────────────────────

output "validation_trail_firehose_stream_name" {
  description = "Nome do Kinesis Data Firehose delivery stream para DecisionTrails"
  value       = module.firehose_decision_trail.firehose_stream_name
}

output "validation_trail_firehose_stream_arn" {
  description = "ARN do Kinesis Data Firehose delivery stream para DecisionTrails"
  value       = module.firehose_decision_trail.firehose_stream_arn
}

output "validation_trail_bucket_name" {
  description = "Nome do bucket S3 de destino para os DecisionTrails em Parquet"
  value       = module.firehose_decision_trail.trail_bucket_name
}

output "validation_trail_error_bucket_name" {
  description = "Nome do bucket S3 de erros do pipeline Firehose de DecisionTrails"
  value       = module.firehose_decision_trail.error_bucket_name
}

output "validation_trail_emitter_policy_arn" {
  description = "ARN da IAM policy de emissão de DecisionTrails — anexar à role do Data Plane quando criada"
  value       = module.firehose_decision_trail.emitter_policy_arn
}

output "validation_trail_glue_database_name" {
  description = "Nome do banco de dados Glue para os DecisionTrails"
  value       = module.firehose_decision_trail.glue_database_name
}

output "validation_trail_glue_table_name" {
  description = "Nome da tabela Glue com o schema do DecisionTrail"
  value       = module.firehose_decision_trail.glue_table_name
}

# ─── Validation Engine — CloudWatch Alarms ────────────────────────────────────

output "validation_alarm_policy_engine_not_ready_arn" {
  description = "ARN do alarme CloudWatch para PolicyEngineNotReady errors"
  value       = module.cloudwatch_validation_alarms.policy_engine_not_ready_alarm_arn
}

output "validation_alarm_refresh_failure_arn" {
  description = "ARN do alarme CloudWatch para falhas de refresh de policy"
  value       = module.cloudwatch_validation_alarms.refresh_failure_alarm_arn
}

output "validation_alarm_integrity_failure_arn" {
  description = "ARN do alarme CloudWatch para falhas de integridade de bundle/snapshot"
  value       = module.cloudwatch_validation_alarms.integrity_failure_alarm_arn
}

output "validation_alarm_emitter_failure_arn" {
  description = "ARN do alarme CloudWatch para falhas de emissão de DecisionTrail"
  value       = module.cloudwatch_validation_alarms.emitter_failure_alarm_arn
}

output "validation_alarm_anomalous_rejections_arn" {
  description = "ARN do alarme CloudWatch para taxa anômala de POLICY_REJECTED"
  value       = module.cloudwatch_validation_alarms.anomalous_rejections_alarm_arn
}
