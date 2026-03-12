# ─────────────────────────────────────────────────────────────────────────────
# Módulo Lambda — Double-Entry Ledger
#
# Cria uma função Lambda com IAM role de least privilege baseada no function_type:
#   - write:           acesso DynamoDB (PutItem, UpdateItem, TransactWriteItems)
#   - read:            acesso DynamoDB (GetItem, Query)
#   - publisher:       acesso DynamoDB Streams + EventBridge PutEvents + SQS (DLQ)
#   - audit_transform: acesso DynamoDB Streams + Firehose PutRecordBatch + SQS (DLQ)
#
# Event Source Mappings são criados automaticamente para publisher e audit_transform.
# ─────────────────────────────────────────────────────────────────────────────

locals {
  # Filtros de Event Source Mapping por tipo de função
  publisher_filter_pattern = jsonencode({
    dynamodb = {
      Keys = {
        PK = {
          S = [{ prefix = "OUTBOX#" }]
        }
      }
    }
  })

  audit_filter_pattern = jsonencode({
    dynamodb = {
      Keys = {
        PK = {
          S = [
            { prefix = "JOURNAL#" },
            { prefix = "ACCOUNT#" }
          ]
        }
      }
    }
  })
}

# ─── IAM Role da Lambda ───────────────────────────────────────────────────────

resource "aws_iam_role" "lambda" {
  name = "${var.function_name}-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "lambda.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }
    ]
  })

  tags = merge(var.tags, { Module = "lambda" })
}

# Política base: CloudWatch Logs (necessária para todas as Lambdas)
resource "aws_iam_role_policy" "cloudwatch_logs" {
  name = "${var.function_name}-cloudwatch-logs"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:*:*:log-group:/aws/lambda/${var.function_name}:*"
      }
    ]
  })
}

# ─── Política DynamoDB para Write Lambda ─────────────────────────────────────

resource "aws_iam_role_policy" "dynamodb_write" {
  count = var.function_type == "write" ? 1 : 0
  name  = "${var.function_name}-dynamodb-write"
  role  = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:GetItem",
          "dynamodb:BatchGetItem",
          "dynamodb:Query",
          "dynamodb:TransactWriteItems"
        ]
        Resource = [
          var.dynamodb_table_arn,
          "${var.dynamodb_table_arn}/index/*"
        ]
      }
    ]
  })
}

# ─── Política DynamoDB para Read Lambda ──────────────────────────────────────

resource "aws_iam_role_policy" "dynamodb_read" {
  count = var.function_type == "read" ? 1 : 0
  name  = "${var.function_name}-dynamodb-read"
  role  = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:Query"
        ]
        Resource = [
          var.dynamodb_table_arn,
          "${var.dynamodb_table_arn}/index/*"
        ]
      }
    ]
  })
}

# ─── Políticas para Publisher Lambda ─────────────────────────────────────────

resource "aws_iam_role_policy" "publisher_stream" {
  count = var.function_type == "publisher" ? 1 : 0
  name  = "${var.function_name}-stream"
  role  = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetRecords",
          "dynamodb:GetShardIterator",
          "dynamodb:DescribeStream",
          "dynamodb:ListStreams"
        ]
        Resource = var.dynamodb_stream_arn
      },
      {
        Effect   = "Allow"
        Action   = ["events:PutEvents"]
        Resource = var.event_bus_arn
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = var.dlq_arn
      }
    ]
  })
}

# ─── Políticas para Audit Transform Lambda ───────────────────────────────────

resource "aws_iam_role_policy" "audit_transform" {
  count = var.function_type == "audit_transform" ? 1 : 0
  name  = "${var.function_name}-audit"
  role  = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetRecords",
          "dynamodb:GetShardIterator",
          "dynamodb:DescribeStream",
          "dynamodb:ListStreams"
        ]
        Resource = var.dynamodb_stream_arn
      },
      {
        Effect   = "Allow"
        Action   = ["firehose:PutRecordBatch"]
        Resource = var.firehose_stream_arn
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = var.dlq_arn
      }
    ]
  })
}

# ─── Função Lambda ────────────────────────────────────────────────────────────

resource "aws_lambda_function" "this" {
  function_name    = var.function_name
  role             = aws_iam_role.lambda.arn
  handler          = var.handler
  runtime          = var.runtime
  filename         = var.filename
  source_code_hash = var.source_code_hash
  memory_size      = var.memory_size
  timeout          = var.timeout

  dynamic "environment" {
    for_each = length(var.environment_variables) > 0 ? [1] : []
    content {
      variables = var.environment_variables
    }
  }

  # DLQ para falhas de invocação assíncrona (write e read não usam DLQ de stream)
  dynamic "dead_letter_config" {
    for_each = var.dlq_arn != "" && contains(["publisher", "audit_transform"], var.function_type) ? [1] : []
    content {
      target_arn = var.dlq_arn
    }
  }

  tags = merge(var.tags, { Module = "lambda", FunctionType = var.function_type })
}

# ─── Event Source Mapping — Publisher Lambda (filtro OUTBOX#) ─────────────────

resource "aws_lambda_event_source_mapping" "publisher_stream" {
  count = var.function_type == "publisher" ? 1 : 0

  event_source_arn                   = var.dynamodb_stream_arn
  function_name                      = aws_lambda_function.this.arn
  starting_position                  = var.event_source_starting_position
  batch_size                         = var.event_source_batch_size
  maximum_batching_window_in_seconds = var.event_source_batching_window
  bisect_batch_on_function_error     = true

  # Filtro: apenas registros com PK prefixo OUTBOX#
  filter_criteria {
    filter {
      pattern = local.publisher_filter_pattern
    }
  }

  # DLQ para registros que falharam após todas as tentativas
  destination_config {
    on_failure {
      destination_arn = var.dlq_arn
    }
  }
}

# ─── Event Source Mapping — Audit Transform Lambda (filtro JOURNAL# e ACCOUNT#)

resource "aws_lambda_event_source_mapping" "audit_stream" {
  count = var.function_type == "audit_transform" ? 1 : 0

  event_source_arn                   = var.dynamodb_stream_arn
  function_name                      = aws_lambda_function.this.arn
  starting_position                  = var.event_source_starting_position
  batch_size                         = var.event_source_batch_size
  maximum_batching_window_in_seconds = var.event_source_batching_window
  bisect_batch_on_function_error     = true

  # Filtro: registros com PK prefixo JOURNAL# ou ACCOUNT#
  filter_criteria {
    filter {
      pattern = local.audit_filter_pattern
    }
  }

  # DLQ separada da DLQ do Publisher
  destination_config {
    on_failure {
      destination_arn = var.dlq_arn
    }
  }
}
