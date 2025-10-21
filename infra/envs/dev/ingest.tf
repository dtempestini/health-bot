locals {
  project_name = "health-bot"
  env          = "dev"
  region       = "us-east-1"

  raw_bucket   = "${local.project_name}-raw-${local.env}"
  events_table = "hb_events_${local.env}"
  lambda_name  = "hb_ingest_${local.env}"
  api_name     = "hb_ingest_api_${local.env}"
}

# --- S3 RAW BUCKET ---
resource "aws_s3_bucket" "raw" {
  bucket = local.raw_bucket
}

resource "aws_s3_bucket_versioning" "raw_v" {
  bucket = aws_s3_bucket.raw.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "raw_sse" {
  bucket = aws_s3_bucket.raw.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# --- DYNAMODB EVENTS TABLE ---
resource "aws_dynamodb_table" "events" {
  name         = local.events_table
  billing_mode = "PAY_PER_REQUEST"

  hash_key  = "pk" # user id or "me"
  range_key = "sk" # ISO8601 timestamp

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  attribute {
    name = "dt"
    type = "S"
  }

  attribute {
    name = "type"
    type = "S"
  }

  global_secondary_index {
    name            = "gsi_dt"
    hash_key        = "dt"
    range_key       = "type"
    projection_type = "ALL"
  }

  tags = {
    Project = local.project_name
    Env     = local.env
  }
}

# --- SNS TOPIC used for meal events ---
resource "aws_sns_topic" "meal_events" {
  name = "hb_meal_events_${local.env}"
  tags = {
    Project = local.project_name
    Env     = local.env
  }
}

# --- Secrets Manager lookup (Nutritionix) ---
# Use data source because the secret already exists out-of-band.
data "aws_secretsmanager_secret" "nutrition_api_key" {
  name = "hb_nutrition_api_key_dev"
}

# --- LAMBDA ROLE + POLICY ---
data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ingest_role" {
  name               = "${local.lambda_name}-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "ingest_policy" {
  statement {
    sid       = "WriteS3"
    actions   = ["s3:PutObject", "s3:PutObjectAcl"]
    resources = ["${aws_s3_bucket.raw.arn}/*"]
  }

  statement {
    sid       = "DDBWriteAndDescribe"
    actions   = ["dynamodb:PutItem", "dynamodb:DescribeTable"]
    resources = [aws_dynamodb_table.events.arn]
  }

  statement {
    sid       = "SNSPublishMeals"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.meal_events.arn]
  }

  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:*:*:*"]
  }

  statement {
    sid       = "ReadNutritionSecretForIngest"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [data.aws_secretsmanager_secret.nutrition_api_key.arn]
  }
}

resource "aws_iam_policy" "ingest_inline" {
  name   = "${local.lambda_name}-policy"
  policy = data.aws_iam_policy_document.ingest_policy.json
}

resource "aws_iam_role_policy_attachment" "ingest_attach" {
  role       = aws_iam_role.ingest_role.name
  policy_arn = aws_iam_policy.ingest_inline.arn
}

# --- LAMBDA FUNCTION ---
resource "aws_lambda_function" "ingest" {
  function_name    = local.lambda_name
  role             = aws_iam_role.ingest_role.arn
  runtime          = "python3.12"
  handler          = "ingest.lambda_handler"
  filename         = "${path.module}/lambda_ingest.zip"
  source_code_hash = filebase64sha256("${path.module}/lambda_ingest.zip")
  timeout          = 10

  environment {
    variables = {
      RAW_BUCKET            = aws_s3_bucket.raw.bucket
      EVENTS_TABLE          = aws_dynamodb_table.events.name
      USER_ID               = "me"
      NUTRITION_SECRET_NAME = data.aws_secretsmanager_secret.nutrition_api_key.name
      MEAL_EVENTS_ARN       = aws_sns_topic.meal_events.arn
      PK_NAME               = "pk"
      SK_NAME               = "sk"
    }
  }

  depends_on = [aws_sns_topic.meal_events]
}

# --- API GATEWAY (HTTP API) + ROUTE ---
resource "aws_apigatewayv2_api" "api" {
  name          = local.api_name
  protocol_type = "HTTP"
}

resource "aws_lambda_permission" "allow_api" {
  statement_id  = "AllowInvokeFromAPIGW"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ingest.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.api.execution_arn}/*/*"
}

resource "aws_apigatewayv2_integration" "ingest" {
  api_id                 = aws_apigatewayv2_api.api.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.ingest.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 10000
}

resource "aws_apigatewayv2_route" "post_ingest" {
  api_id    = aws_apigatewayv2_api.api.id
  route_key = "POST /ingest"
  target    = "integrations/${aws_apigatewayv2_integration.ingest.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.api.id
  name        = "$default"
  auto_deploy = true
}

output "ingest_url" {
  value = "${aws_apigatewayv2_api.api.api_endpoint}/ingest"
}
