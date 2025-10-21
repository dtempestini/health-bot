locals {
  stats_lambda_name = "hb_stats_api_dev"
  stats_api_name    = "hb_stats_http_api_dev"
}

# Reuse the assume-role policy from ingest.tf
# data "aws_iam_policy_document" "lambda_assume" already exists

resource "aws_iam_role" "stats_role" {
  name               = "${local.stats_lambda_name}-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json

  tags = { app = "health-bot", stack = "dev", part = "stats" }
}

# Read-only access to the four tables
data "aws_iam_policy_document" "stats_ro" {
  statement {
    sid     = "ReadMeals"
    actions = ["dynamodb:Query","dynamodb:GetItem","dynamodb:DescribeTable"]
    resources = [aws_dynamodb_table.hb_meals_dev.arn]
  }
  statement {
    sid     = "ReadTotals"
    actions = ["dynamodb:Query","dynamodb:GetItem","dynamodb:DescribeTable"]
    resources = [aws_dynamodb_table.hb_daily_totals_dev.arn]
  }
    statement {
        sid     = "ReadMeds"
        actions = ["dynamodb:Query","dynamodb:GetItem","dynamodb:DescribeTable"]
        resources = [
            aws_dynamodb_table.hb_meds_dev.arn,
            "${aws_dynamodb_table.hb_meds_dev.arn}/index/*"
        ]
    }

    statement {
        sid     = "ReadMigraines"
        actions = ["dynamodb:Query","dynamodb:GetItem","dynamodb:DescribeTable"]
        resources = [
            aws_dynamodb_table.hb_migraines_dev.arn,
            "${aws_dynamodb_table.hb_migraines_dev.arn}/index/*"
        ]
    }
  statement {
    sid     = "Logs"
    actions = ["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"]
    resources = ["arn:aws:logs:*:*:*"]
  }
}

resource "aws_iam_policy" "stats_ro" {
  name   = "${local.stats_lambda_name}-ro"
  policy = data.aws_iam_policy_document.stats_ro.json
}

resource "aws_iam_role_policy_attachment" "stats_attach" {
  role       = aws_iam_role.stats_role.name
  policy_arn = aws_iam_policy.stats_ro.arn
}

# Lambda (zip is built in buildspec; placed alongside others)
resource "aws_lambda_function" "stats" {
  function_name    = local.stats_lambda_name
  role             = aws_iam_role.stats_role.arn
  handler          = "stats_api.lambda_handler"
  runtime          = "python3.12"
  architectures    = ["x86_64"]
  filename         = "${path.module}/lambda_stats_api.zip"
  source_code_hash = filebase64sha256("${path.module}/lambda_stats_api.zip")
  timeout          = 15
  memory_size      = 256

  environment {
    variables = {
      USER_ID         = "me"
      MEALS_TABLE     = aws_dynamodb_table.hb_meals_dev.name
      TOTALS_TABLE    = aws_dynamodb_table.hb_daily_totals_dev.name
      MIGRAINES_TABLE = aws_dynamodb_table.hb_migraines_dev.name
      MEDS_TABLE      = aws_dynamodb_table.hb_meds_dev.name
      CALORIES_MAX    = "1800"
      PROTEIN_MIN     = "210"
    }
  }

  tags = { app = "health-bot", stack = "dev", part = "stats" }
}

# HTTP API for /stats/*
resource "aws_apigatewayv2_api" "stats_api" {
  name          = local.stats_api_name
  protocol_type = "HTTP"

  # Optional API-level CORS (Lambda also returns CORS headers)
  cors_configuration {
    allow_origins = ["*"]
    allow_methods = ["GET","OPTIONS"]
    allow_headers = ["*"]
  }
}

resource "aws_lambda_permission" "allow_api_stats" {
  statement_id  = "AllowInvokeFromAPIGWStats"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.stats.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.stats_api.execution_arn}/*/*"
}

resource "aws_apigatewayv2_integration" "stats_integration" {
  api_id                 = aws_apigatewayv2_api.stats_api.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.stats.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 10000
}

# One greedy route that covers /stats/* (incl. /stats/health)
resource "aws_apigatewayv2_route" "stats_route" {
  api_id    = aws_apigatewayv2_api.stats_api.id
  route_key = "ANY /stats/{proxy+}"
  target    = "integrations/${aws_apigatewayv2_integration.stats_integration.id}"
}

resource "aws_apigatewayv2_stage" "stats_stage" {
  api_id      = aws_apigatewayv2_api.stats_api.id
  name        = "$default"
  auto_deploy = true
}

output "stats_base_url" {
  value = aws_apigatewayv2_api.stats_api.api_endpoint
}
