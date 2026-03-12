# ─────────────────────────────────────────────────────────────────────────────
# Módulo cloudwatch-validation-alarms — Alarmes CloudWatch para o Validation Engine
#
# Provisiona 5 alarmes mínimos para o runtime do Validation Engine:
#
#   1. policy_engine_not_ready  — PolicyEngineNotReady errors spike
#   2. refresh_failure          — falhas de refresh de policy
#   3. integrity_failure        — falhas de integridade de bundle/snapshot
#   4. emitter_failure          — falhas de emissão de DecisionTrail
#   5. anomalous_rejections     — taxa anômala de POLICY_REJECTED
#
# Todos os alarmes usam o namespace configurável (padrão: "ValidationEngine").
# As métricas devem ser publicadas pelo runtime do Validation Engine com os
# nomes de métrica correspondentes.
#
# Requisitos cobertos: 18.3, 18.5, 22.4, 22.7
# ─────────────────────────────────────────────────────────────────────────────

# ─── Alarme 1: policy_engine_not_ready ───────────────────────────────────────
#
# Dispara quando PolicyEngineNotReady errors aparecem.
# Indica cold start sem ActivePolicySet válido — sistema fail-closed.
# Qualquer ocorrência deve ser investigada imediatamente.
#
# Requisito 17.1: cold start sem policy válida → 503 + alerta operacional
# Requisito 18.5: métricas específicas para falhas de bootstrap

resource "aws_cloudwatch_metric_alarm" "policy_engine_not_ready" {
  alarm_name          = "${var.name_prefix}-policy-engine-not-ready"
  alarm_description   = "PolicyEngineNotReady errors detectados — runtime sem ActivePolicySet válido. Investigar imediatamente."
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = var.engine_not_ready_evaluation_periods
  metric_name         = "PolicyEngineNotReadyErrors"
  namespace           = var.namespace
  period              = var.engine_not_ready_period_seconds
  statistic           = "Sum"
  threshold           = var.engine_not_ready_threshold

  # treat_missing_data = "notBreaching" — ausência de dados não é alarme
  # (o runtime pode não estar publicando métricas se não houver tráfego)
  treat_missing_data = "notBreaching"

  alarm_actions = var.alarm_actions
  ok_actions    = var.ok_actions

  tags = merge(var.tags, {
    Module    = "cloudwatch-validation-alarms"
    AlarmType = "policy-engine-not-ready"
  })
}

# ─── Alarme 2: refresh_failure ────────────────────────────────────────────────
#
# Dispara quando falhas de refresh de policy se acumulam.
# Indica problemas com AppConfig, S3 ou rede no plano de controle.
# O runtime usa LKG enquanto o refresh falha — mas falhas persistentes
# significam que o sistema está operando com policy desatualizada.
#
# Requisito 17.2: LKG usado após falha de refresh — mas deve ser monitorado
# Requisito 18.5: métricas específicas para falhas de refresh

resource "aws_cloudwatch_metric_alarm" "refresh_failure" {
  alarm_name          = "${var.name_prefix}-refresh-failure"
  alarm_description   = "Falhas de refresh de policy detectadas — runtime pode estar usando Last Known Good desatualizado."
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = var.refresh_failure_evaluation_periods
  metric_name         = "PolicyRefreshFailures"
  namespace           = var.namespace
  period              = var.refresh_failure_period_seconds
  statistic           = "Sum"
  threshold           = var.refresh_failure_threshold

  treat_missing_data = "notBreaching"

  alarm_actions = var.alarm_actions
  ok_actions    = var.ok_actions

  tags = merge(var.tags, {
    Module    = "cloudwatch-validation-alarms"
    AlarmType = "refresh-failure"
  })
}

# ─── Alarme 3: integrity_failure ──────────────────────────────────────────────
#
# Dispara quando falhas de integridade de bundle ou snapshot são detectadas.
# Indica possível adulteração de artefatos ou corrupção de dados no S3.
# Qualquer ocorrência deve ser investigada como incidente de segurança.
#
# Requisito 17.4: falha de integridade → rejeitar bundle + alerta operacional
# Requisito 20.4: verificação criptográfica de integridade antes da ativação

resource "aws_cloudwatch_metric_alarm" "integrity_failure" {
  alarm_name          = "${var.name_prefix}-integrity-failure"
  alarm_description   = "Falha de integridade de bundle/snapshot detectada — possível adulteração de artefatos. Investigar como incidente de segurança."
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = var.integrity_failure_evaluation_periods
  metric_name         = "PolicyBundleIntegrityFailures"
  namespace           = var.namespace
  period              = var.integrity_failure_period_seconds
  statistic           = "Sum"
  threshold           = var.integrity_failure_threshold

  treat_missing_data = "notBreaching"

  alarm_actions = var.alarm_actions
  ok_actions    = var.ok_actions

  tags = merge(var.tags, {
    Module    = "cloudwatch-validation-alarms"
    AlarmType = "integrity-failure"
  })
}

# ─── Alarme 4: emitter_failure ────────────────────────────────────────────────
#
# Dispara quando falhas de emissão de DecisionTrail se acumulam.
# O emitter é best-effort — falhas não afetam transações, mas indicam
# que a trilha de auditoria expandida está incompleta.
# Falhas persistentes devem ser investigadas para garantir auditabilidade.
#
# Requisito 13.4: falha de emissão não invalida transação, mas deve ser logada
# Requisito 18.5: métricas específicas para falhas de emissão de DecisionTrail

resource "aws_cloudwatch_metric_alarm" "emitter_failure" {
  alarm_name          = "${var.name_prefix}-emitter-failure"
  alarm_description   = "Falhas de emissão de DecisionTrail ao Firehose — trilha de auditoria expandida pode estar incompleta."
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = var.emitter_failure_evaluation_periods
  metric_name         = "DecisionTrailEmissionFailures"
  namespace           = var.namespace
  period              = var.emitter_failure_period_seconds
  statistic           = "Sum"
  threshold           = var.emitter_failure_threshold

  treat_missing_data = "notBreaching"

  alarm_actions = var.alarm_actions
  ok_actions    = var.ok_actions

  tags = merge(var.tags, {
    Module    = "cloudwatch-validation-alarms"
    AlarmType = "emitter-failure"
  })
}

# ─── Alarme 5: anomalous_rejections ──────────────────────────────────────────
#
# Dispara quando a taxa de POLICY_REJECTED é anomalamente alta.
# Usa uma métrica calculada (math expression) para calcular a taxa de rejeição:
#   rejection_rate = (PolicyRejections / TotalEvaluations) * 100
#
# Uma taxa alta pode indicar:
# - Mudança de policy muito restritiva recém-ativada
# - Ataque ou comportamento anômalo de clientes
# - Bug na DSL ou no evaluator
#
# Requisito 18.3: métricas para total de avaliações, aprovações e rejeições
# Requisito 22.4: alarmes para aumento anômalo de POLICY_REJECTED

resource "aws_cloudwatch_metric_alarm" "anomalous_rejections" {
  alarm_name          = "${var.name_prefix}-anomalous-rejections"
  alarm_description   = "Taxa de POLICY_REJECTED anomalamente alta — verificar policy recém-ativada, comportamento de clientes ou bug no evaluator."
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = var.anomalous_rejections_evaluation_periods
  threshold           = var.anomalous_rejections_threshold

  treat_missing_data = "notBreaching"

  # Métrica calculada: taxa de rejeição em percentual
  # rejection_rate = (PolicyRejections / TotalEvaluations) * 100
  metric_query {
    id          = "rejection_rate"
    expression  = "(rejections / total_evaluations) * 100"
    label       = "Rejection Rate (%)"
    return_data = true
  }

  metric_query {
    id = "rejections"
    metric {
      metric_name = "PolicyRejections"
      namespace   = var.namespace
      period      = var.anomalous_rejections_period_seconds
      stat        = "Sum"
    }
  }

  metric_query {
    id = "total_evaluations"
    metric {
      metric_name = "TotalEvaluations"
      namespace   = var.namespace
      period      = var.anomalous_rejections_period_seconds
      stat        = "Sum"
    }
  }

  alarm_actions = var.alarm_actions
  ok_actions    = var.ok_actions

  tags = merge(var.tags, {
    Module    = "cloudwatch-validation-alarms"
    AlarmType = "anomalous-rejections"
  })
}
