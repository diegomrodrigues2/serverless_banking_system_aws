# ─────────────────────────────────────────────────────────────────────────────
# outputs.tf — Outputs do módulo cloudwatch-validation-alarms
# ─────────────────────────────────────────────────────────────────────────────

output "policy_engine_not_ready_alarm_arn" {
  description = "ARN do alarme CloudWatch para PolicyEngineNotReady errors"
  value       = aws_cloudwatch_metric_alarm.policy_engine_not_ready.arn
}

output "refresh_failure_alarm_arn" {
  description = "ARN do alarme CloudWatch para falhas de refresh de policy"
  value       = aws_cloudwatch_metric_alarm.refresh_failure.arn
}

output "integrity_failure_alarm_arn" {
  description = "ARN do alarme CloudWatch para falhas de integridade de bundle/snapshot"
  value       = aws_cloudwatch_metric_alarm.integrity_failure.arn
}

output "emitter_failure_alarm_arn" {
  description = "ARN do alarme CloudWatch para falhas de emissão de DecisionTrail"
  value       = aws_cloudwatch_metric_alarm.emitter_failure.arn
}

output "anomalous_rejections_alarm_arn" {
  description = "ARN do alarme CloudWatch para taxa anômala de POLICY_REJECTED"
  value       = aws_cloudwatch_metric_alarm.anomalous_rejections.arn
}

output "all_alarm_arns" {
  description = "Lista com os ARNs de todos os alarmes do módulo"
  value = [
    aws_cloudwatch_metric_alarm.policy_engine_not_ready.arn,
    aws_cloudwatch_metric_alarm.refresh_failure.arn,
    aws_cloudwatch_metric_alarm.integrity_failure.arn,
    aws_cloudwatch_metric_alarm.emitter_failure.arn,
    aws_cloudwatch_metric_alarm.anomalous_rejections.arn,
  ]
}
