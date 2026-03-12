# ─────────────────────────────────────────────────────────────────────────────
# Módulo Firehose + Glue — Audit Pipeline para o Double-Entry Ledger
#
# Cria:
#   1. Glue Catalog Database + Table com schema do AuditRecord
#   2. IAM Role para o Firehose (acesso a S3 destino, S3 erros e Glue Catalog)
#   3. Kinesis Data Firehose delivery stream com:
#      - Destino extended_s3 (bucket de auditoria WORM)
#      - Buffer: 128MB ou 60s
#      - Data format conversion: JSON → Parquet via Glue Table schema
#      - Dynamic Partitioning: year/month/day/tenant via JQ extraction
#      - Error output: bucket de erros separado
#
# O Firehose gerencia automaticamente:
#   - Batching e buffering
#   - Conversão JSON → Parquet (via Glue Table schema)
#   - Particionamento dinâmico no S3
#   - Compressão Snappy
#   - Retry e entrega garantida
# ─────────────────────────────────────────────────────────────────────────────

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# ─── Glue Catalog Database ────────────────────────────────────────────────────

resource "aws_glue_catalog_database" "audit" {
  name        = var.glue_database_name
  description = "Banco de dados Glue para registros de auditoria do Double-Entry Ledger"
}

# ─── Glue Catalog Table — Schema do AuditRecord ───────────────────────────────
#
# Schema flat conforme AuditRecord em audit_exporter.py:
#   record_type, entry_id, external_id, entry_type, account_id, amount,
#   direction, currency, posting_index, tenant_id, timestamp, metadata,
#   year, month, day

resource "aws_glue_catalog_table" "audit_records" {
  name          = var.glue_table_name
  database_name = aws_glue_catalog_database.audit.name
  description   = "Schema dos registros de auditoria contábil (JournalEntries e Postings)"

  table_type = "EXTERNAL_TABLE"

  parameters = {
    "classification"      = "parquet"
    "parquet.compression" = "SNAPPY"
    "EXTERNAL"            = "TRUE"
  }

  storage_descriptor {
    location      = "s3://${split(":::", var.audit_bucket_arn)[1]}/audit/"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    ser_de_info {
      name                  = "parquet-serde"
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
      parameters = {
        "serialization.format" = "1"
      }
    }

    # Colunas do AuditRecord (campos não-particionados)
    columns {
      name    = "record_type"
      type    = "string"
      comment = "JOURNAL_ENTRY ou POSTING"
    }
    columns {
      name    = "entry_id"
      type    = "string"
      comment = "UUID do JournalEntry"
    }
    columns {
      name    = "external_id"
      type    = "string"
      comment = "Chave de idempotência"
    }
    columns {
      name    = "entry_type"
      type    = "string"
      comment = "STANDARD ou REVERSAL"
    }
    columns {
      name    = "account_id"
      type    = "string"
      comment = "ID da conta (presente apenas para POSTING)"
    }
    columns {
      name    = "amount"
      type    = "bigint"
      comment = "Valor em minor units (presente apenas para POSTING)"
    }
    columns {
      name    = "direction"
      type    = "string"
      comment = "DEBIT ou CREDIT (presente apenas para POSTING)"
    }
    columns {
      name    = "currency"
      type    = "string"
      comment = "Código ISO 4217 (presente apenas para POSTING)"
    }
    columns {
      name    = "posting_index"
      type    = "int"
      comment = "Índice ordinal do posting (presente apenas para POSTING)"
    }
    columns {
      name    = "tenant_id"
      type    = "string"
      comment = "ID do tenant para particionamento dinâmico"
    }
    columns {
      name    = "timestamp"
      type    = "string"
      comment = "ISO 8601 do fato contábil"
    }
    columns {
      name    = "metadata"
      type    = "string"
      comment = "JSON serializado dos metadados do JournalEntry"
    }
  }

  # Colunas de particionamento (extraídas pelo Firehose Dynamic Partitioning)
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
    name    = "tenant"
    type    = "string"
    comment = "tenant_id para particionamento por cliente"
  }
}

# ─── IAM Role para o Firehose ─────────────────────────────────────────────────

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

  tags = merge(var.tags, { Module = "firehose-audit" })
}

resource "aws_iam_role_policy" "firehose" {
  name = "${var.stream_name}-firehose-policy"
  role = aws_iam_role.firehose.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # Acesso ao bucket de auditoria WORM (destino principal)
      {
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
          var.audit_bucket_arn,
          "${var.audit_bucket_arn}/*"
        ]
      },
      # Acesso ao bucket de erros
      {
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
          var.error_bucket_arn,
          "${var.error_bucket_arn}/*"
        ]
      },
      # Acesso ao Glue Catalog para conversão JSON → Parquet
      {
        Effect = "Allow"
        Action = [
          "glue:GetTable",
          "glue:GetTableVersion",
          "glue:GetTableVersions"
        ]
        Resource = [
          "arn:aws:glue:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:catalog",
          "arn:aws:glue:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:database/${var.glue_database_name}",
          "arn:aws:glue:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:table/${var.glue_database_name}/${var.glue_table_name}"
        ]
      },
      # CloudWatch Logs para monitoramento do Firehose
      {
        Effect = "Allow"
        Action = [
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:log-group:/aws/kinesisfirehose/${var.stream_name}:*"
      }
    ]
  })
}

# ─── Kinesis Data Firehose Delivery Stream ────────────────────────────────────

resource "aws_kinesis_firehose_delivery_stream" "audit" {
  name        = var.stream_name
  destination = "extended_s3"

  extended_s3_configuration {
    role_arn   = aws_iam_role.firehose.arn
    bucket_arn = var.audit_bucket_arn

    # Buffer: 128MB ou 60s (o que vier primeiro)
    buffering_size     = var.buffer_size_mb
    buffering_interval = var.buffer_interval_seconds

    # Prefixo S3 com Dynamic Partitioning
    # Firehose extrai year/month/day do timestamp e tenant_id via JQ
    prefix = "audit/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/tenant=!{partitionKeyFromQuery:tenant_id}/"

    # Prefixo para registros com erro de entrega
    error_output_prefix = "errors/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/!{firehose:error-output-type}/"

    # Conversão JSON → Parquet via Glue Table schema
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
        database_name = aws_glue_catalog_database.audit.name
        table_name    = aws_glue_catalog_table.audit_records.name
        region        = data.aws_region.current.id
        version_id    = "LATEST"
      }
    }

    # Dynamic Partitioning — extrai tenant_id via JQ para particionamento por cliente
    dynamic_partitioning_configuration {
      enabled        = true
      retry_duration = 300
    }

    # Processamento JQ para extrair tenant_id do registro JSON
    processing_configuration {
      enabled = true

      processors {
        type = "MetadataExtraction"

        parameters {
          parameter_name  = "MetadataExtractionQuery"
          parameter_value = "{tenant_id:.tenant_id}"
        }

        parameters {
          parameter_name  = "JsonParsingEngine"
          parameter_value = "JQ-1.6"
        }
      }
    }

    # Backup habilitado — registros com falha na conversão Parquet vão para o bucket de erros
    s3_backup_mode = "Enabled"

    s3_backup_configuration {
      role_arn   = aws_iam_role.firehose.arn
      bucket_arn = var.error_bucket_arn
      prefix     = "backup/"

      buffering_size     = 5
      buffering_interval = 300
    }
  }

  tags = merge(var.tags, { Module = "firehose-audit" })
}
