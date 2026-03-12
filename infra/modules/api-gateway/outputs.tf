output "api_id" {
  description = "ID do HTTP API Gateway"
  value       = aws_apigatewayv2_api.this.id
}

output "api_endpoint" {
  description = "URL base do API Gateway (ex: https://<id>.execute-api.<region>.amazonaws.com)"
  value       = aws_apigatewayv2_api.this.api_endpoint
}

output "execution_arn" {
  description = "ARN de execução do API Gateway (usado em aws_lambda_permission)"
  value       = aws_apigatewayv2_api.this.execution_arn
}
