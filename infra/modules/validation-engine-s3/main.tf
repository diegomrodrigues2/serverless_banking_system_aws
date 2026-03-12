# ─────────────────────────────────────────────────────────────────────────────
# Módulo validation-engine-s3 — Storage WORM para o Validation Engine
#
# Cria dois buckets:
#
#   1. bundles_bucket (bucket principal):
#      - Object Lock em modo GOVERNANCE (WORM) — artefatos imutáveis
#      - Versionamento obrigatório (pré-requisito do Object Lock)
#      - SSE-KMS com chave gerenciada pelo cliente (CMK)
#      - Prefixos lógicos: bundles/ e snapshots/
#      - Acesso público bloqueado
#      - Lifecycle: Standard → Standard-IA → Glacier IR
#      - Bucket policy com IAM least privilege para leitores e escritores
#
#   2. error_bucket:
#      - Bucket dedicado para registros de erro do pipeline de artefatos
#      - SSE-KMS com a mesma CMK
#      - Sem Object Lock (registros de erro podem ser reprocessados/deletados)
#      - Acesso público bloqueado
#      - Lifecycle simples com expiração
#
# IAM:
#   - aws_iam_policy.reader: GetObject + GetObjectVersion nos prefixos bundles/ e snapshots/
#   - aws_iam_policy.writer: PutObject + GetObject + GetObjectVersion nos mesmos prefixos
#   - Políticas são criadas e podem ser anexadas a roles externas via outputs
#
# Requisitos cobertos: 3.1, 3.2, 3.6, 20.1, 22.1, 22.7
# ─────────────────────────────────────────────────────────────────────────────

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# ─── Bucket Principal (bundles + snapshots) ───────────────────────────────────

resource "aws_s3_bucket" "bundles" {
  bucket = var.bundles_bucket_name

  # Object Lock requer que o bucket seja criado com object_lock_enabled = true.
  # Não é possível habilitar Object Lock em bucket existente sem recriação.
  object_lock_enabled = true

  tags = merge(var.tags, {
    Module     = "validation-engine-s3"
    BucketType = "bundles-worm"
  })
}

# Bloquear todo acesso público — artefatos de policy nunca devem ser públicos
resource "aws_s3_bucket_public_access_block" "bundles" {
  bucket = aws_s3_bucket.bundles.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Versionamento obrigatório — pré-requisito do Object Lock e requisito 3.6
resource "aws_s3_bucket_versioning" "bundles" {
  bucket = aws_s3_bucket.bundles.id

  versioning_configuration {
    status = "Enabled"
  }
}

# SSE-KMS com chave gerenciada pelo cliente — requisito 3.6 e 20.1
# bucket_key_enabled = true reduz chamadas ao KMS e custo operacional
resource "aws_s3_bucket_server_side_encryption_configuration" "bundles" {
  bucket = aws_s3_bucket.bundles.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

# Object Lock — WORM para imutabilidade dos artefatos compilados
# GOVERNANCE: admins com s3:BypassGovernanceRetention podem remover em casos excepcionais
resource "aws_s3_bucket_object_lock_configuration" "bundles" {
  bucket = aws_s3_bucket.bundles.id

  rule {
    default_retention {
      mode = var.object_lock_mode
      days = var.object_lock_retention_days
    }
  }

  # Garante que versionamento esteja ativo antes de configurar Object Lock
  depends_on = [aws_s3_bucket_versioning.bundles]
}

# Lifecycle — transição de storage class para reduzir custo de artefatos históricos
resource "aws_s3_bucket_lifecycle_configuration" "bundles" {
  bucket = aws_s3_bucket.bundles.id

  depends_on = [aws_s3_bucket_versioning.bundles]

  # Tiering para bundles compilados
  rule {
    id     = "bundles-tiering"
    status = "Enabled"

    filter {
      prefix = "bundles/"
    }

    transition {
      days          = var.transition_to_ia_days
      storage_class = "STANDARD_IA"
    }

    transition {
      days          = var.transition_to_glacier_days
      storage_class = "GLACIER_IR"
    }
  }

  # Tiering para snapshots de referência
  rule {
    id     = "snapshots-tiering"
    status = "Enabled"

    filter {
      prefix = "snapshots/"
    }

    transition {
      days          = var.transition_to_ia_days
      storage_class = "STANDARD_IA"
    }

    transition {
      days          = var.transition_to_glacier_days
      storage_class = "GLACIER_IR"
    }
  }

  # Expirar versões não-correntes para controlar custo de versionamento
  rule {
    id     = "expire-old-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }
}

# Bucket policy — nega qualquer acesso sem SSE-KMS e bloqueia transporte não-TLS
# Least privilege: apenas os principals declarados nas variáveis têm acesso
resource "aws_s3_bucket_policy" "bundles" {
  bucket = aws_s3_bucket.bundles.id

  # Aguarda o bloqueio de acesso público antes de aplicar a policy
  depends_on = [aws_s3_bucket_public_access_block.bundles]

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # Nega qualquer requisição sem TLS — dados de policy em trânsito devem ser criptografados
      {
        Sid       = "DenyNonTLS"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.bundles.arn,
          "${aws_s3_bucket.bundles.arn}/*"
        ]
        Condition = {
          Bool = {
            "aws:SecureTransport" = "false"
          }
        }
      },
      # Nega uploads sem SSE-KMS — garante que todos os objetos usem a CMK configurada
      {
        Sid       = "DenyNonKMSUploads"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.bundles.arn}/*"
        Condition = {
          StringNotEquals = {
            "s3:x-amz-server-side-encryption" = "aws:kms"
          }
        }
      }
    ]
  })
}

# ─── Bucket de Erros ──────────────────────────────────────────────────────────

resource "aws_s3_bucket" "errors" {
  bucket = var.error_bucket_name

  tags = merge(var.tags, {
    Module     = "validation-engine-s3"
    BucketType = "validation-errors"
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

# SSE-KMS no bucket de erros — mesma CMK para consistência de controle de acesso
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

# Bucket policy — nega transporte não-TLS no bucket de erros
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
          Bool = {
            "aws:SecureTransport" = "false"
          }
        }
      }
    ]
  })
}

# ─── IAM Policy — Leitura (Data Plane / Runtime) ──────────────────────────────
#
# Permissões mínimas para o runtime do Validation Engine ler bundles e snapshots.
# Inclui acesso KMS para descriptografar objetos SSE-KMS.

resource "aws_iam_policy" "reader" {
  name        = "${var.bundles_bucket_name}-reader"
  description = "Leitura least-privilege de RuleBundles e ReferenceSnapshots no bucket ${var.bundles_bucket_name}"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # Leitura de objetos nos prefixos bundles/ e snapshots/
      {
        Sid    = "ReadBundlesAndSnapshots"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:GetObjectVersion"
        ]
        Resource = [
          "${aws_s3_bucket.bundles.arn}/bundles/*",
          "${aws_s3_bucket.bundles.arn}/snapshots/*"
        ]
      },
      # ListBucket restrito aos prefixos necessários para verificação de existência
      {
        Sid    = "ListBundlesAndSnapshots"
        Effect = "Allow"
        Action = [
          "s3:ListBucket"
        ]
        Resource = aws_s3_bucket.bundles.arn
        Condition = {
          StringLike = {
            "s3:prefix" = ["bundles/*", "snapshots/*"]
          }
        }
      },
      # Descriptografia KMS — necessária para objetos SSE-KMS
      {
        Sid    = "KMSDecrypt"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey"
        ]
        Resource = var.kms_key_arn
      }
    ]
  })

  tags = merge(var.tags, { Module = "validation-engine-s3" })
}

# ─── IAM Policy — Escrita (Control Plane) ─────────────────────────────────────
#
# Permissões mínimas para o Control Plane armazenar bundles e snapshots compilados.
# Inclui acesso KMS para criptografar objetos SSE-KMS.

resource "aws_iam_policy" "writer" {
  name        = "${var.bundles_bucket_name}-writer"
  description = "Escrita least-privilege de RuleBundles e ReferenceSnapshots no bucket ${var.bundles_bucket_name}"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # Escrita e leitura de objetos nos prefixos bundles/ e snapshots/
      {
        Sid    = "WriteBundlesAndSnapshots"
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject",
          "s3:GetObjectVersion"
        ]
        Resource = [
          "${aws_s3_bucket.bundles.arn}/bundles/*",
          "${aws_s3_bucket.bundles.arn}/snapshots/*"
        ]
      },
      # ListBucket restrito aos prefixos necessários para idempotência (verificar existência antes de escrever)
      {
        Sid    = "ListBundlesAndSnapshots"
        Effect = "Allow"
        Action = [
          "s3:ListBucket"
        ]
        Resource = aws_s3_bucket.bundles.arn
        Condition = {
          StringLike = {
            "s3:prefix" = ["bundles/*", "snapshots/*"]
          }
        }
      },
      # Criptografia e descriptografia KMS — necessária para PutObject e GetObject com SSE-KMS
      {
        Sid    = "KMSEncryptDecrypt"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey"
        ]
        Resource = var.kms_key_arn
      }
    ]
  })

  tags = merge(var.tags, { Module = "validation-engine-s3" })
}

# ─── Anexar policy de leitura aos principals declarados ───────────────────────

resource "aws_iam_role_policy_attachment" "reader" {
  for_each = toset(var.reader_principal_arns)

  # Extrai o nome da role do ARN (último segmento após "/")
  role       = element(split("/", each.value), length(split("/", each.value)) - 1)
  policy_arn = aws_iam_policy.reader.arn
}

# ─── Anexar policy de escrita aos principals declarados ───────────────────────

resource "aws_iam_role_policy_attachment" "writer" {
  for_each = toset(var.writer_principal_arns)

  role       = element(split("/", each.value), length(split("/", each.value)) - 1)
  policy_arn = aws_iam_policy.writer.arn
}
