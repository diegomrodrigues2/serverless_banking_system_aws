# ─────────────────────────────────────────────────────────────────────────────
# Módulo firehose-decision-trail — Pipeline de analytics para DecisionTrails
#
# Cria:
#   1. Bucket S3 de destino (trail_bucket) com SSE-KMS e versionamento
#   2. Bucket S3 de erros (error_bucket) com SSE-KMS
#   3. Glue Catalog Database + Table com schema do DecisionTrail
#   4. IAM Role para o Firehose (acesso a S3, Glue, KMS)
#   5. Kinesis Data Firehose delivery stream com:
#      - Destino extended_s3 (trail_bucket)
#      - Formato Parquet + Snappy via Glue Table schema
#      - Dynamic Partitioning: year/month/day/tenant_id/policy_scope_id
#      - Bucket de erros dedicado
#      - SSE-KMS ponta a ponta
#   6. IAM Policy de emissão (least privilege) para o Data Plane
#
# Particionamento S3 (Requisito 21.4):
#   trails/year=YYYY/month=MM/day=DD/tenant_id=X/policy_scope_id=Y/
#
# Requisitos cobertos: 13.5, 21.1, 21.3, 21.4, 21.5, 22.3, 22.7
# ─────────────────────────────────────────────────────────────────────────────

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# ─── Bucket de Destino (DecisionTrails) ──────────────────────────────────────

resource "aws_s3_bucket" "trails" {
  bucket = var.trail_bucket_name

  tags = merge(var.tags, {
    Module     = "firehose-decision-trail"
    BucketType = "decision-trails"
  })
}

resource "aws_s3_bucket_public_access_block" "trails" {
  bucket = aws_s3_bucket.trails.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "trails" {
  bucket = aws_s3_bucket.trails.id

  versioning_configuration {
    status = "Enabled"
  }
}

# SSE-KMS com chave gerenciada pelo cliente — Requisito 20.2, 22.7
resource "aws_s3_bucket_server_side_encryption_configuration" "trails" {
  bucket = aws_s3_bucket.trails.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

# Lifecycle: transição para Glacier IR após retenção configurável
resource "aws_s3_bucket_lifecycle_configuration" "trails" {
  bucket = aws_s3_bucket.trails.id

  depends_on = [aws_s3_bucket_versioning.trails]

  rule {
    id     = "trails-tiering"
    status = "Enabled"

    filter {
      prefix = "trails/"
    }

    transition {
      days          = var.trail_retention_days
      storage_class = "GLACIER_IR"
    }

    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }
}

# Bucket policy — nega transporte não-TLS e uploads sem SSE-KMS
resource "aws_s3_bucket_policy" "trails" {
  bucket = aws_s3_bucket.trails.id

  depends_on = [aws_s3_bucket_public_access_block.trails]

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyNonTLS"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.trails.arn,
          "${aws_s3_bucket.trails.arn}/*"
        ]
        Condition = {
          Bool = { "aws:SecureTransport" = "false" }
        }
      },
      {
        Sid       = "DenyNonKMSUploads"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.trails.arn}/*"
        Condition = {
          StringNotEquals = { "s3:x-amz-server-side-encryption" = "aws:kms" }
        }
      }
    ]
  })
}

# ─── Bucket de Erros ──────────────────────────────────────────────────────────

resource "aws_s3_bucket" "errors" {
  bucket = var.error_bucket_name

  tags = merge(var.tags, {
    Module     = "firehose-decision-trail"
    BucketType = "firehose-errors"
  })
}

resource "aws_s3_bucket_public_access_block" "errors" {
  bucket = aws_s3_bucket.errors.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "errors" {
  bucket = aws_s3_bucket.errors.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "errors" {
  bucket = aws_s3_bucket.errors.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "errors" {
  bucket = aws_s3_bucket.errors.id

  depends_on = [aws_s3_bucket_versioning.errors]

  rule {
    id     = "expire-error-records"
    status = "Enabled"

    filter {}

    expiration {
      days = var.error_retention_days
    }

    noncurrent_version_expiration {
      noncurrent_days = 7
    }
  }
}

resource "aws_s3_bucket_policy" "errors" {
  bucket = aws_s3_bucket.errors.id

  depends_on = [aws_s3_bucket_public_access_block.errors]

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyNonTLS"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.errors.arn,
          "${aws_s3_bucket.errors.arn}/*"
        ]
        Condition = {
          Bool = { "aws:SecureTransport" = "false" }
        }
      }
    ]
  })
}

# ─── Glue Catalog Database ────────────────────────────────────────────────────

resource "aws_glue_catalog_database" "trails" {
  name        = var.glue_database_name
  description = "Banco de dados Glue para DecisionTrails do Validation Engine"
}

# ─── Glue Catalog Table — Schema do DecisionTrail ─────────────────────────────
#
# Schema flat conforme DecisionTrail.to_firehose_payload() em models.py:
#   external_id, tenant_id, policy_scope_id, activation_id, artifact_hash,
#   snapshot_version, evaluator_version, input_hash, final_verdict,
#   matched_deny_rule, rules (JSON), evaluation_latency_ms, error_code, timestamp
#
# Colunas de particionamento (extraídas pelo Firehose Dynamic Partitioning):
#   year, month, day, tenant_id, policy_scope_id

resource "aws_glue_catalog_table" "decision_trails" {
  name          = var.glue_table_name
  database_name = aws_glue_catalog_database.trails.name
  description   = "Schema dos DecisionTrails do Validation Engine (Parquet/Snappy)"

  table_type = "EXTERNAL_TABLE"

  parameters = {
    "classification"      = "parquet"
    "parquet.compression" = "SNAPPY"
    "EXTERNAL"            = "TRUE"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.trails.bucket}/trails/"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    ser_de_info {
      name                  = "parquet-serde"
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
      parameters = {
        "serialization.format" = "1"
      }
    }

    # Campos não-particionados do DecisionTrail
    columns {
      name    = "external_id"
      type    = "string"
      comment = "Identificador externo da transação (fornecido pelo chamador da API)"
    }
    columns {
      name    = "activation_id"
      type    = "string"
      comment = "Identificador da ativação do PolicyActivationManifest"
    }
    columns {
      name    = "artifact_hash"
      type    = "string"
      comment = "SHA-256 do RuleBundle usado na avaliação"
    }
    columns {
      name    = "snapshot_version"
      type    = "string"
      comment = "Versão do ReferenceSnapshot usado na avaliação"
    }
    columns {
      name    = "evaluator_version"
      type    = "string"
      comment = "Versão do RuleEvaluator usado na avaliação"
    }
    columns {
      name    = "input_hash"
      type    = "string"
      comment = "Hash do CanonicalValidationContext para verificação de integridade"
    }
    columns {
      name    = "final_verdict"
      type    = "string"
      comment = "APPROVED ou REJECTED"
    }
    columns {
      name    = "matched_deny_rule"
      type    = "string"
      comment = "Nome da rule DENY que determinou a rejeição (nullable)"
    }
    columns {
      name    = "rules"
      type    = "string"
      comment = "Lista de RuleMatchResults serializada como JSON"
    }
    columns {
      name    = "evaluation_latency_ms"
      type    = "double"
      comment = "Latência da avaliação em milissegundos"
    }
    columns {
      name    = "error_code"
      type    = "string"
      comment = "Código de erro se a avaliação falhou internamente (nullable)"
    }
    columns {
      name    = "timestamp"
      type    = "string"
      comment = "Timestamp ISO 8601 da avaliação"
    }
  }

  # Colunas de particionamento — extraídas pelo Firehose Dynamic Partitioning
  partition_keys {
    name    = "year"
    type    = "string"
    comment = "Ano extraído do timestamp"
  }
  partition_keys {
    name    = "month"
    type    = "string"
    comment = "Mês extraído do timestamp"
  }
  partition_keys {
    name    = "day"
    type    = "string"
    comment = "Dia extraído do timestamp"
  }
  partition_keys {
    name    = "tenant_id"
    type    = "string"
    comment = "tenant_id para particionamento por cliente"
  }
  partition_keys {
    name    = "policy_scope_id"
    type    = "string"
    comment = "policy_scope_id para particionamento por escopo de policy"
  }
}

# ─── IAM Role para o Firehose ─────────────────────────────────────────────────
#
# Role assumida pelo Firehose para acessar S3, Glue e KMS.
# Least privilege: acesso restrito aos recursos deste módulo.

resource "aws_iam_role" "firehose" {
  name = "${var.stream_name}-firehose-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "firehose.amazonaws.com" }
        Action    = "sts:AssumeRole"
        Condition = {
          StringEquals = {
            "sts:ExternalId" = data.aws_caller_identity.current.account_id
          }
        }
      }
    ]
  })

  tags = merge(var.tags, { Module = "firehose-decision-trail" })
}

resource "aws_iam_role_policy" "firehose" {
  name = "${var.stream_name}-firehose-policy"
  role = aws_iam_role.firehose.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # Acesso ao bucket de trails (destino principal)
      {
        Sid    = "S3TrailsBucket"
        Effect = "Allow"
        Action = [
          "s3:AbortMultipartUpload",
          "s3:GetBucketLocation",
          "s3:GetObject",
          "s3:ListBucket",
          "s3:ListBucketMultipartUploads",
          "s3:PutObject"
        ]
        Resource = [
          aws_s3_bucket.trails.arn,
          "${aws_s3_bucket.trails.arn}/*"
        ]
      },
      # Acesso ao bucket de erros
      {
        Sid    = "S3ErrorsBucket"
        Effect = "Allow"
        Action = [
          "s3:AbortMultipartUpload",
          "s3:GetBucketLocation",
          "s3:GetObject",
          "s3:ListBucket",
          "s3:ListBucketMultipartUploads",
          "s3:PutObject"
        ]
        Resource = [
          aws_s3_bucket.errors.arn,
          "${aws_s3_bucket.errors.arn}/*"
        ]
      },
      # Acesso ao Glue Catalog para conversão JSON → Parquet
      {
        Sid    = "GlueCatalogAccess"
        Effect = "Allow"
        Action = [
          "glue:GetTable",
          "glue:GetTableVersion",
          "glue:GetTableVersions"
        ]
        Resource = [
          "arn:aws:glue:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:catalog",
          "arn:aws:glue:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:database/${var.glue_database_name}",
          "arn:aws:glue:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:table/${var.glue_database_name}/${var.glue_table_name}"
        ]
      },
      # KMS para criptografia/descriptografia dos objetos S3
      {
        Sid    = "KMSAccess"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey"
        ]
        Resource = var.kms_key_arn
      },
      # CloudWatch Logs para monitoramento do Firehose
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = ["logs:PutLogEvents"]
        Resource = "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:/aws/kinesisfirehose/${var.stream_name}:*"
      }
    ]
  })
}

# ─── Kinesis Data Firehose Delivery Stream ────────────────────────────────────
#
# Stream dedicado para DecisionTrails do Validation Engine.
# Converte JSON → Parquet/Snappy via Glue Table schema.
# Particiona por year/month/day/tenant_id/policy_scope_id via Dynamic Partitioning.
#
# Requisito 21.3: formato colunar (Parquet + Snappy)
# Requisito 21.4: particionamento por year/month/day/tenant_id/policy_scope_id

resource "aws_kinesis_firehose_delivery_stream" "decision_trails" {
  name        = var.stream_name
  destination = "extended_s3"

  # SSE-KMS ponta a ponta no stream — Requisito 20.2
  server_side_encryption {
    enabled  = true
    key_type = "CUSTOMER_MANAGED_CMK"
    key_arn  = var.kms_key_arn
  }

  extended_s3_configuration {
    role_arn   = aws_iam_role.firehose.arn
    bucket_arn = aws_s3_bucket.trails.arn

    buffering_size     = var.buffer_size_mb
    buffering_interval = var.buffer_interval_seconds

    # Prefixo S3 com Dynamic Partitioning por year/month/day/tenant_id/policy_scope_id
    # Firehose extrai os campos via JQ do payload JSON do DecisionTrail
    prefix = "trails/year=!{partitionKeyFromQuery:year}/month=!{partitionKeyFromQuery:month}/day=!{partitionKeyFromQuery:day}/tenant_id=!{partitionKeyFromQuery:tenant_id}/policy_scope_id=!{partitionKeyFromQuery:policy_scope_id}/"

    # Prefixo para registros com erro de entrega ou conversão
    error_output_prefix = "errors/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/!{firehose:error-output-type}/"

    # Conversão JSON → Parquet via Glue Table schema — Requisito 21.3
    data_format_conversion_configuration {
      enabled = true

      input_format_configuration {
        deserializer {
          open_x_json_ser_de {}
        }
      }

      output_format_configuration {
        serializer {
          parquet_ser_de {
            compression = "SNAPPY"
          }
        }
      }

      schema_configuration {
        role_arn      = aws_iam_role.firehose.arn
        database_name = aws_glue_catalog_database.trails.name
        table_name    = aws_glue_catalog_table.decision_trails.name
        region        = data.aws_region.current.name
        version_id    = "LATEST"
      }
    }

    # Dynamic Partitioning — extrai campos do payload JSON para particionamento
    dynamic_partitioning_configuration {
      enabled        = true
      retry_duration = 300
    }

    # Processamento JQ para extrair campos de particionamento do DecisionTrail
    # Extrai: year, month, day (do timestamp), tenant_id e policy_scope_id
    processing_configuration {
      enabled = true

      processors {
        type = "MetadataExtraction"

        parameters {
          parameter_name  = "MetadataExtractionQuery"
          parameter_value = "{year: .timestamp[0:4], month: .timestamp[5:7], day: .timestamp[8:10], tenant_id: .tenant_id, policy_scope_id: .policy_scope_id}"
        }

        parameters {
          parameter_name  = "JsonParsingEngine"
          parameter_value = "JQ-1.6"
        }
      }
    }

    # Backup para o bucket de erros — registros com falha na conversão Parquet
    # Requisito 21.5: área de erro dedicada para reprocessamento
    s3_backup_mode = "Enabled"

    s3_backup_configuration {
      role_arn   = aws_iam_role.firehose.arn
      bucket_arn = aws_s3_bucket.errors.arn
      prefix     = "backup/"

      buffering_size     = 5
      buffering_interval = 300
    }
  }

  tags = merge(var.tags, { Module = "firehose-decision-trail" })
}

# ─── IAM Policy — Emissão (Data Plane / Runtime) ──────────────────────────────
#
# Permissões mínimas para o runtime do Validation Engine emitir DecisionTrails.
# Inclui acesso KMS para criptografar registros no stream.

resource "aws_iam_policy" "emitter" {
  name        = "${var.stream_name}-emitter"
  description = "Emissão least-privilege de DecisionTrails ao Firehose (Data Plane)"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # PutRecord e PutRecordBatch para emissão de trails
      {
        Sid    = "FirehosePutRecord"
        Effect = "Allow"
        Action = [
          "firehose:PutRecord",
          "firehose:PutRecordBatch"
        ]
        Resource = aws_kinesis_firehose_delivery_stream.decision_trails.arn
      },
      # KMS para criptografar registros no stream SSE-KMS
      {
        Sid    = "KMSEncrypt"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey"
        ]
        Resource = var.kms_key_arn
      }
    ]
  })

  tags = merge(var.tags, { Module = "firehose-decision-trail" })
}

# ─── Anexar policy de emissão aos principals declarados ───────────────────────

resource "aws_iam_role_policy_attachment" "emitter" {
  for_each = toset(var.emitter_principal_arns)

  role       = element(split("/", each.value), length(split("/", each.value)) - 1)
  policy_arn = aws_iam_policy.emitter.arn
}
