# Módulo: cloudwatch-validation-alarms

Alarmes CloudWatch para o runtime do Validation Engine.

## Alarmes provisionados

| Alarme | Métrica | Descrição |
|---|---|---|
| `policy_engine_not_ready` | `PolicyEngineNotReadyErrors` | Erros de cold start sem ActivePolicySet válido |
| `refresh_failure` | `PolicyRefreshFailures` | Falhas de refresh de policy (runtime usando LKG) |
| `integrity_failure` | `PolicyBundleIntegrityFailures` | Falhas de integridade de bundle/snapshot |
| `emitter_failure` | `DecisionTrailEmissionFailures` | Falhas de emissão de DecisionTrail ao Firehose |
| `anomalous_rejections` | `PolicyRejections / TotalEvaluations` | Taxa anômala de POLICY_REJECTED |

## Métricas esperadas

O runtime do Validation Engine deve publicar as seguintes métricas no namespace configurado (padrão: `ValidationEngine`):

- `PolicyEngineNotReadyErrors` — incrementado a cada erro `PolicyEngineNotReady`
- `PolicyRefreshFailures` — incrementado a cada falha de refresh de policy
- `PolicyBundleIntegrityFailures` — incrementado a cada falha de integridade
- `DecisionTrailEmissionFailures` — incrementado a cada falha de emissão
- `PolicyRejections` — incrementado a cada `POLICY_REJECTED`
- `TotalEvaluations` — incrementado a cada avaliação (aprovada ou rejeitada)

## Uso

```hcl
module "cloudwatch_validation_alarms" {
  source = "../../modules/cloudwatch-validation-alarms"

  name_prefix   = "ledger-validation-dev"
  namespace     = "ValidationEngine"
  alarm_actions = []  # adicionar SNS topic ARN em staging/prod

  tags = local.common_tags
}
```

## Requisitos

| Requisito | Descrição |
|---|---|
| 18.3 | Métricas para avaliações, aprovações, rejeições e falhas |
| 18.5 | Métricas específicas para falhas de integridade, refresh, emissão e bootstrap |
| 22.4 | Terraform provisiona alarmes e métricas mínimas |
| 22.7 | IAM least privilege por componente |
