# ─────────────────────────────────────────────────────────────────────────────
# Módulo S3 Audit — Armazenamento WORM para registros contábeis
#
# Cria dois buckets:
#   1. audit_bucket: destino principal do Firehose com Object Lock (WORM)
#      - Versionamento obrigatório (pré-requisito do Object Lock)
#      - Criptografia SSE-S3 server-side
#      - Lifecycle: Standard → Standard-IA → Glacier Instant Retrieval
#      - Acesso público bloqueado
#
#   2. error_bucket: registros que falharam na conversão Parquet do Firehose
#      - Sem Object Lock (registros de erro podem ser reprocessados/deletados)
#      - Criptografia SSE-S3
#      - Acesso público bloqueado
# ─────────────────────────────────────────────────────────────────────────────

# ─── Bucket de Auditoria WORM ─────────────────────────────────────────────────

resource "aws_s3_bucket" "audit" {
  bucket = var.audit_bucket_name

  # Object Lock requer que o bucket seja criado com object_lock_enabled = true
  object_lock_enabled = true

  tags = merge(var.tags, { Module = "s3-audit", BucketType = "audit-worm" })
}

# Bloquear todo acesso público — dados financeiros nunca devem ser públicos
resource "aws_s3_bucket_public_access_block" "audit" {
  bucket = aws_s3_bucket.audit.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Versionamento obrigatório para Object Lock
resource "aws_s3_bucket_versioning" "audit" {
  bucket = aws_s3_bucket.audit.id

  versioning_configuration {
    status = "Enabled"
  }
}

# Criptografia server-side com SSE-S3
resource "aws_s3_bucket_server_side_encryption_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

# Object Lock — WORM para compliance financeiro
# GOVERNANCE: admins com s3:BypassGovernanceRetention podem remover em casos legais
# COMPLIANCE: ninguém pode remover antes do prazo (mais restritivo)
resource "aws_s3_bucket_object_lock_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id

  rule {
    default_retention {
      mode = var.object_lock_mode
      days = var.object_lock_retention_days
    }
  }
}

# Lifecycle: transição de storage class para reduzir custo de dados históricos
resource "aws_s3_bucket_lifecycle_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id

  # Aguarda versionamento estar ativo antes de aplicar lifecycle
  depends_on = [aws_s3_bucket_versioning.audit]

  rule {
    id     = "audit-tiering"
    status = "Enabled"

    filter {
      prefix = "audit/"
    }

    # Transição para Standard-IA após 90 dias (acesso infrequente)
    transition {
      days          = var.transition_to_ia_days
      storage_class = "STANDARD_IA"
    }

    # Transição para Glacier Instant Retrieval após 1 ano
    transition {
      days          = var.transition_to_glacier_days
      storage_class = "GLACIER_IR"
    }
  }

  rule {
    id     = "expire-old-versions"
    status = "Enabled"

    filter {}

    # Remove versões não-correntes após 90 dias para controlar custo
    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }
}

# ─── Bucket de Erros do Firehose ──────────────────────────────────────────────

resource "aws_s3_bucket" "errors" {
  bucket = var.error_bucket_name

  tags = merge(var.tags, { Module = "s3-audit", BucketType = "firehose-errors" })
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
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

# Lifecycle simples para o bucket de erros — expirar após 30 dias
resource "aws_s3_bucket_lifecycle_configuration" "errors" {
  bucket = aws_s3_bucket.errors.id

  depends_on = [aws_s3_bucket_versioning.errors]

  rule {
    id     = "expire-error-records"
    status = "Enabled"

    filter {}

    expiration {
      days = 30
    }

    noncurrent_version_expiration {
      noncurrent_days = 7
    }
  }
}
