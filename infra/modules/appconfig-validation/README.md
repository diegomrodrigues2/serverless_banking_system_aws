# appconfig-validation

Módulo Terraform para provisionamento do AppConfig do Validation Engine.

## Responsabilidade

Provisiona a infraestrutura AppConfig necessária para publicar e resolver `PolicyActivationManifests` por escopo de policy.

## Recursos criados

| Recurso | Descrição |
|---|---|
| `aws_appconfig_application` | Application dedicada ao Validation Engine |
| `aws_appconfig_environment` | Environment por ambiente (dev/staging/prod) |
| `aws_appconfig_configuration_profile` | Profile freeform com validator JSON Schema |
| `aws_appconfig_deployment_strategy` | Estratégia por ambiente (criada quando `deployment_strategy_name` é null) |
| `aws_iam_policy.reader` | Leitura least-privilege para o Data Plane |
| `aws_iam_policy.publisher` | Publicação least-privilege para o Control Plane |

## Estrutura do manifesto

```json
{
  "version": "1",
  "scopes": {
    "tenantA:TRANSFER:PIX:*:prod": {
      "activation_id": "act_2026_03_11_001",
      "artifact_hash": "sha256:...",
      "snapshot_version": "snap_2026_03_11_001",
      "context_schema_version": "1.0",
      "evaluator_version": "1.2.0"
    }
  }
}
```

## Estratégia de deployment por ambiente

| Ambiente | growth_factor | duration | bake |
|---|---|---|---|
| dev | 100 | 0 | 0 |
| staging | 10 | 10 | 2 |
| prod | 10 | 30 | 5 |

## Uso

```hcl
module "appconfig_validation" {
  source = "../../modules/appconfig-validation"

  application_name  = "ledger-validation-engine-dev"
  environment_name  = "dev"
  kms_key_arn       = aws_kms_key.validation_engine.arn

  # dev: all-at-once
  deployment_duration_in_minutes = 0
  growth_factor                  = 100
  final_bake_time_in_minutes     = 0

  reader_principal_arns    = []
  publisher_principal_arns = []

  tags = local.common_tags
}
```

## Requisitos cobertos

- Requisito 4.1: PolicyActivationManifest publicado via AppConfig
- Requisito 22.2: AppConfig provisionado via Terraform
- Requisito 22.7: IAM least privilege por componente
