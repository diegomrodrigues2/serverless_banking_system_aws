output "function_arn" {
  description = "ARN da função Lambda"
  value       = aws_lambda_function.this.arn
}

output "function_name" {
  description = "Nome da função Lambda"
  value       = aws_lambda_function.this.function_name
}

output "role_arn" {
  description = "ARN da IAM role da função Lambda"
  value       = aws_iam_role.lambda.arn
}

output "role_name" {
  description = "Nome da IAM role da função Lambda"
  value       = aws_iam_role.lambda.name
}

output "invoke_arn" {
  description = "Invoke ARN da função Lambda (usado em integrações do API Gateway)"
  value       = aws_lambda_function.this.invoke_arn
}
