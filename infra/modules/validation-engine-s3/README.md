# Módulo: validation-engine-s3

Storage WORM para o Validation Engine — armazena `RuleBundle` e `ReferenceSnapshot` de forma imutável, auditável e criptografada.

## Recursos criados

| Recurso | Descrição |
|---|---|
| `aws_s3_bucket.bundles` | Bucket principal com Object Lock (WORM), versionamento e SSE-KMS |
| `aws_s3_bucket.errors` | Bucket dedicado para registros de erro do pipeline de artefatos |
| `aws_iam_policy.reader` | Policy de leitura least-privilege para o Data Plane (runtime) |
| `aws_iam_policy.writer` | Policy de escrita least-privilege para o Control Plane |

## Prefixos lógicos no bucket principal

| Prefixo | Conteúdo |
|---|---|
| `bundles/` | `RuleBundle` compilados, indexados por `artifact_hash` |
| `snapshots/` | `ReferenceSnapshot` imutáveis, indexados por `snapshot_version` |

## Segurança

- **Object Lock (WORM)**: modo `GOVERNANCE` por padrão. Nenhum artefato publicado pode ser alterado retroativamente.
- **SSE-KMS**: criptografia com chave gerenciada pelo cliente (CMK). Todos os uploads sem SSE-KMS são negados pela bucket policy.
- **TLS obrigatório**: bucket policy nega qualquer requisição sem `aws:SecureTransport`.
- **Acesso público bloqueado**: todos os quatro controles de bloqueio público estão habilitados.
- **IAM least privilege**: policies separadas para leitura (Data Plane) e escrita (Control Plane), restritas aos prefixos `bundles/` e `snapshots/`.

## Uso

```hcl
module "validation_engine_s3" {
  source = "../../modules/validation-engine-s3"

  bundles_bucket_name = "my-project-validation-bundles-dev"
  error_bucket_name   = "my-project-validation-errors-dev"
  kms_key_arn         = aws_kms_key.validation_engine.arn

  # Principals que recebem permissão de leitura (Data Plane / runtime Lambda)
  reader_principal_arns = [aws_iam_role.write_lambda.arn]

  # Principals que recebem permissão de escrita (Control Plane / CI pipeline)
  writer_principal_arns = [aws_iam_role.control_plane.arn]

  tags = {
    Environment = "dev"
    Project     = "my-project"
    ManagedBy   = "terraform"
  }
}
```

## Inputs

| Nome | Tipo | Obrigatório | Padrão | Descrição |
|---|---|---|---|---|
| `bundles_bucket_name` | `string` | sim | — | Nome do bucket principal |
| `error_bucket_name` | `string` | sim | — | Nome do bucket de erros |
| `kms_key_arn` | `string` | sim | — | ARN da CMK KMS para SSE-KMS |
| `object_lock_mode` | `string` | não | `"GOVERNANCE"` | Modo WORM: `GOVERNANCE` ou `COMPLIANCE` |
| `object_lock_retention_days` | `number` | não | `2555` | Retenção WORM em dias (~7 anos) |
| `reader_principal_arns` | `list(string)` | não | `[]` | ARNs de roles/users com permissão de leitura |
| `writer_principal_arns` | `list(string)` | não | `[]` | ARNs de roles/users com permissão de escrita |
| `transition_to_ia_days` | `number` | não | `180` | Dias até transição para Standard-IA |
| `transition_to_glacier_days` | `number` | não | `730` | Dias até transição para Glacier IR |
| `tags` | `map(string)` | não | `{}` | Tags aplicadas a todos os recursos |

## Outputs

| Nome | Descrição |
|---|---|
| `bundles_bucket_name` | Nome do bucket principal |
| `bundles_bucket_arn` | ARN do bucket principal |
| `error_bucket_name` | Nome do bucket de erros |
| `error_bucket_arn` | ARN do bucket de erros |
| `reader_policy_arn` | ARN da IAM policy de leitura (anexar à role do runtime) |
| `writer_policy_arn` | ARN da IAM policy de escrita (anexar à role do Control Plane) |

## Requisitos cobertos

- **3.1**: Armazenamento de `RuleBundle` em S3 com Object Lock (WORM)
- **3.2**: Armazenamento de `ReferenceSnapshot` em S3 com Object Lock (WORM)
- **3.6**: Buckets com KMS Envelope Encryption e versionamento habilitado
- **20.1**: Object Lock em modo GOVERNANCE e criptografia KMS
- **22.1**: Terraform provisiona buckets S3 com Object Lock, versionamento e KMS
- **22.7**: IAM least privilege para cada componente

## Pré-requisitos

- A chave KMS (`kms_key_arn`) deve existir antes de aplicar este módulo.
- O provider AWS deve ser configurado no root module — este módulo não contém blocos `provider`.
- Terraform >= 1.10.0 e AWS provider ~> 5.80.
