# Módulo: firehose-decision-trail

Pipeline de analytics para `DecisionTrail` do Validation Engine.

## Recursos criados

| Recurso | Descrição |
|---|---|
| `aws_s3_bucket.trails` | Bucket S3 de destino para DecisionTrails em Parquet/Snappy |
| `aws_s3_bucket.errors` | Bucket S3 dedicado para registros de erro do Firehose |
| `aws_glue_catalog_database.trails` | Banco de dados Glue para os DecisionTrails |
| `aws_glue_catalog_table.decision_trails` | Tabela Glue com schema do DecisionTrail |
| `aws_iam_role.firehose` | IAM Role assumida pelo Firehose |
| `aws_kinesis_firehose_delivery_stream.decision_trails` | Firehose com Parquet + Dynamic Partitioning |
| `aws_iam_policy.emitter` | IAM Policy de emissão least-privilege para o Data Plane |

## Particionamento S3

Os DecisionTrails são particionados no S3 por:

```
trails/year=YYYY/month=MM/day=DD/tenant_id=X/policy_scope_id=Y/
```

O Firehose extrai os campos de particionamento via JQ do payload JSON do `DecisionTrail`.

## Schema do DecisionTrail

| Campo | Tipo Glue | Descrição |
|---|---|---|
| `external_id` | string | Identificador externo da transação |
| `activation_id` | string | Identificador da ativação do manifesto |
| `artifact_hash` | string | SHA-256 do RuleBundle |
| `snapshot_version` | string | Versão do ReferenceSnapshot |
| `evaluator_version` | string | Versão do RuleEvaluator |
| `input_hash` | string | Hash do CanonicalValidationContext |
| `final_verdict` | string | APPROVED ou REJECTED |
| `matched_deny_rule` | string | Rule DENY que rejeitou (nullable) |
| `rules` | string | Lista de RuleMatchResults em JSON |
| `evaluation_latency_ms` | double | Latência da avaliação em ms |
| `error_code` | string | Código de erro interno (nullable) |
| `timestamp` | string | Timestamp ISO 8601 da avaliação |

Colunas de particionamento: `year`, `month`, `day`, `tenant_id`, `policy_scope_id`

## Uso

```hcl
module "firehose_decision_trail" {
  source = "../../modules/firehose-decision-trail"

  stream_name       = "validation-engine-decision-trail-dev"
  trail_bucket_name = "ledger-validation-trails-dev"
  error_bucket_name = "ledger-validation-trail-errors-dev"
  kms_key_arn       = aws_kms_key.validation_engine.arn

  emitter_principal_arns = []

  tags = local.common_tags
}
```

## Requisitos

| Requisito | Descrição |
|---|---|
| 13.5 | DecisionTrail enviado ao pipeline de analytics |
| 21.1 | Pipeline assíncrono para armazenamento analítico |
| 21.3 | Formato colunar (Parquet + Snappy) |
| 21.4 | Particionamento por year/month/day/tenant_id/policy_scope_id |
| 21.5 | Área de erro dedicada para reprocessamento |
| 22.3 | Terraform provisiona pipeline de ingestão |
| 22.7 | IAM least privilege por componente |
