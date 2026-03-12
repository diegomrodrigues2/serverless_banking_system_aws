# ─────────────────────────────────────────────────────────────────────────────
# Módulo API Gateway — Double-Entry Ledger
#
# Cria um HTTP API (API Gateway v2) com integração Lambda proxy para as funções
# write e read. Rotas:
#   POST /entries                  → write lambda
#   POST /reversals                → write lambda
#   GET  /balances/{account_id}    → read lambda
#   GET  /statements/{account_id}  → read lambda
#
# Segurança:
#   - Throttling configurável por stage
#   - Logs de acesso habilitados no CloudWatch
#   - Permissões Lambda concedidas via aws_lambda_permission (least privilege)
# ─────────────────────────────────────────────────────────────────────────────

# ─── HTTP API ─────────────────────────────────────────────────────────────────

resource "aws_apigatewayv2_api" "this" {
  name          = var.api_name
  protocol_type = "HTTP"
  description   = "Double-Entry Ledger HTTP API"

  cors_configuration {
    allow_origins = var.cors_allow_origins
    allow_methods = ["GET", "POST", "OPTIONS"]
    allow_headers = ["Content-Type", "Authorization"]
    max_age       = 300
  }

  tags = merge(var.tags, { Module = "api-gateway" })
}

# ─── CloudWatch Log Group para access logs ────────────────────────────────────

resource "aws_cloudwatch_log_group" "api_access_logs" {
  name              = "/aws/apigateway/${var.api_name}"
  retention_in_days = var.log_retention_days

  tags = merge(var.tags, { Module = "api-gateway" })
}

# ─── Stage com throttling e access logs ───────────────────────────────────────

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.this.id
  name        = "$default"
  auto_deploy = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_access_logs.arn
    format = jsonencode({
      requestId      = "$context.requestId"
      sourceIp       = "$context.identity.sourceIp"
      httpMethod     = "$context.httpMethod"
      routeKey       = "$context.routeKey"
      status         = "$context.status"
      responseLength = "$context.responseLength"
      integrationError = "$context.integrationErrorMessage"
    })
  }

  default_route_settings {
    throttling_burst_limit = var.throttling_burst_limit
    throttling_rate_limit  = var.throttling_rate_limit
  }

  tags = merge(var.tags, { Module = "api-gateway" })
}

# ─── Integrações Lambda ───────────────────────────────────────────────────────

# Integração com a Write Lambda (POST /entries e POST /reversals)
resource "aws_apigatewayv2_integration" "write" {
  api_id                 = aws_apigatewayv2_api.this.id
  integration_type       = "AWS_PROXY"
  integration_uri        = var.write_lambda_invoke_arn
  payload_format_version = "2.0"
}

# Integração com a Read Lambda (GET /balances e GET /statements)
resource "aws_apigatewayv2_integration" "read" {
  api_id                 = aws_apigatewayv2_api.this.id
  integration_type       = "AWS_PROXY"
  integration_uri        = var.read_lambda_invoke_arn
  payload_format_version = "2.0"
}

# ─── Rotas ────────────────────────────────────────────────────────────────────

resource "aws_apigatewayv2_route" "post_entries" {
  api_id    = aws_apigatewayv2_api.this.id
  route_key = "POST /entries"
  target    = "integrations/${aws_apigatewayv2_integration.write.id}"
}

resource "aws_apigatewayv2_route" "post_reversals" {
  api_id    = aws_apigatewayv2_api.this.id
  route_key = "POST /reversals"
  target    = "integrations/${aws_apigatewayv2_integration.write.id}"
}

resource "aws_apigatewayv2_route" "get_balances" {
  api_id    = aws_apigatewayv2_api.this.id
  route_key = "GET /balances/{account_id}"
  target    = "integrations/${aws_apigatewayv2_integration.read.id}"
}

resource "aws_apigatewayv2_route" "get_statements" {
  api_id    = aws_apigatewayv2_api.this.id
  route_key = "GET /statements/{account_id}"
  target    = "integrations/${aws_apigatewayv2_integration.read.id}"
}

# ─── Permissões Lambda ────────────────────────────────────────────────────────
# Permite que o API Gateway invoque cada Lambda. Source ARN restrito ao API
# específico para evitar confused deputy.

resource "aws_lambda_permission" "apigw_write" {
  statement_id  = "AllowAPIGatewayInvokeWrite"
  action        = "lambda:InvokeFunction"
  function_name = var.write_lambda_function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.this.execution_arn}/*/*"
}

resource "aws_lambda_permission" "apigw_read" {
  statement_id  = "AllowAPIGatewayInvokeRead"
  action        = "lambda:InvokeFunction"
  function_name = var.read_lambda_function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.this.execution_arn}/*/*"
}
