##
## PHASE 2: Meal Enricher + Daily Totals + WhatsApp replies
## Tables: hb_meals_dev, hb_daily_totals_dev, hb_migraines_dev, hb_meds_dev, hb_fasting_dev
## Lambda: hb_meal_enricher_dev
## SNS sub: hb_meal_events_dev -> hb_meal_enricher_dev
##

############################
# DYNAMODB TABLES
############################

# --- DynamoDB: custom food overrides ---
resource "aws_dynamodb_table" "hb_food_overrides_dev" {
  name         = "hb_food_overrides_dev"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    app   = "health-bot"
    stack = "dev"
    part  = "food_overrides"
  }
}

resource "aws_dynamodb_table" "hb_meals_dev" {
  name         = "hb_meals_dev"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    app   = "health-bot"
    stack = "dev"
    part  = "meals"
  }
}

# Daily totals (pattern: pk="total#me", sk="YYYY-MM-DD")
resource "aws_dynamodb_table" "hb_daily_totals_dev" {
  name         = "hb_daily_totals_dev"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    app   = "health-bot"
    stack = "dev"
    part  = "totals"
  }
}

################################
# DynamoDB for migraines
################################
resource "aws_dynamodb_table" "hb_migraines_dev" {
  name         = "hb_migraines_dev"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

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
  } # YYYY-MM-DD for start date

  attribute {
    name = "is_open"
    type = "N"
  } # 1 if not ended

  # Find open episode quickly
  global_secondary_index {
    name            = "gsi_open"
    hash_key        = "pk"
    range_key       = "is_open"
    projection_type = "ALL"
  }

  # Query by date (e.g., week/month views)
  global_secondary_index {
    name            = "gsi_dt"
    hash_key        = "dt"
    range_key       = "sk"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    app   = "health-bot"
    stack = "dev"
    part  = "migraines"
  }
}

################################
# DynamoDB for medications
################################
resource "aws_dynamodb_table" "hb_meds_dev" {
  name         = "hb_meds_dev"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk" # user id
  range_key    = "sk" # "dt#<ms>"

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
  } # YYYY-MM-DD

  global_secondary_index {
    name            = "gsi_dt"
    hash_key        = "dt"
    range_key       = "sk"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    app   = "health-bot"
    stack = "dev"
    part  = "meds"
  }
}

################################
# NEW: DynamoDB for fasting
################################
resource "aws_dynamodb_table" "hb_fasting_dev" {
  name         = "hb_fasting_dev"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  } # user id

  attribute {
    name = "sk"
    type = "S"
  } # "fast#<id>"

  attribute {
    name = "dt"
    type = "S"
  } # YYYY-MM-DD (start date)

  attribute {
    name = "is_open"
    type = "N"
  } # 1 when ongoing

  # open fast lookup
  global_secondary_index {
    name            = "gsi_open"
    hash_key        = "pk"
    range_key       = "is_open"
    projection_type = "ALL"
  }

  # date queries (week/month summaries)
  global_secondary_index {
    name            = "gsi_dt"
    hash_key        = "dt"
    range_key       = "sk"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    app   = "health-bot"
    stack = "dev"
    part  = "fasting"
  }
}

############################
# LOOKUPS FOR EXISTING RESOURCES
############################

# SNS created in ingest.tf (hb_meal_events_dev)
data "aws_sns_topic" "hb_meal_events_dev" {
  name = "hb_meal_events_dev"
}

# Events table created in ingest.tf
data "aws_dynamodb_table" "hb_events_dev" {
  name = "hb_events_dev"
}

############################
# IAM (ROLE + POLICY)
############################

# Reuse the assume-role policy declared in ingest.tf:
# data.aws_iam_policy_document.lambda_assume

resource "aws_iam_role" "hb_meal_enricher_dev" {
  name               = "hb-meal-enricher-dev"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json

  tags = {
    app   = "health-bot"
    stack = "dev"
  }
}

resource "aws_iam_role_policy_attachment" "hb_meal_enricher_logs" {
  role       = aws_iam_role.hb_meal_enricher_dev.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "meal_enricher_access" {
  statement {
    sid       = "MealsRW"
    actions   = ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:DescribeTable"]
    resources = [aws_dynamodb_table.hb_meals_dev.arn]
  }

  statement {
    sid       = "FoodOverridesRW"
    actions   = ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:DeleteItem", "dynamodb:Query", "dynamodb:DescribeTable", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.hb_food_overrides_dev.arn]
  }

  statement {
    sid       = "TotalsRW"
    actions   = ["dynamodb:UpdateItem", "dynamodb:GetItem", "dynamodb:DescribeTable"]
    resources = [aws_dynamodb_table.hb_daily_totals_dev.arn]
  }

  statement {
    sid       = "EventsWrite"
    actions   = ["dynamodb:PutItem"]
    resources = [data.aws_dynamodb_table.hb_events_dev.arn]
  }

  statement {
    sid       = "SecretsRead"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["*"]
  }

  statement {
    sid = "MedsRW"
    actions = [
      "dynamodb:PutItem",
      "dynamodb:GetItem",
      "dynamodb:Query",
      "dynamodb:DescribeTable",
      "dynamodb:UpdateItem"
    ]
    resources = [
      aws_dynamodb_table.hb_meds_dev.arn,
      "${aws_dynamodb_table.hb_meds_dev.arn}/index/*"
    ]
  }

  statement {
    sid = "MigrainesRW"
    actions = [
      "dynamodb:PutItem",
      "dynamodb:GetItem",
      "dynamodb:UpdateItem",
      "dynamodb:Query",
      "dynamodb:DeleteItem",
      "dynamodb:DescribeTable"
    ]
    resources = [
      aws_dynamodb_table.hb_migraines_dev.arn,
      "${aws_dynamodb_table.hb_migraines_dev.arn}/index/*"
    ]
  }

  # NEW: fasting permissions (table + all GSIs)
  statement {
    sid = "FastingRW"
    actions = [
      "dynamodb:PutItem",
      "dynamodb:GetItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:Query",
      "dynamodb:DescribeTable",
      "dynamodb:BatchWriteItem"
    ]
    resources = [
      aws_dynamodb_table.hb_fasting_dev.arn,
      "${aws_dynamodb_table.hb_fasting_dev.arn}/index/*"
    ]
  }
}

resource "aws_iam_policy" "meal_enricher_access" {
  name   = "meal-enricher-access-dev"
  policy = data.aws_iam_policy_document.meal_enricher_access.json
}

resource "aws_iam_role_policy_attachment" "meal_enricher_access_attach" {
  role       = aws_iam_role.hb_meal_enricher_dev.name
  policy_arn = aws_iam_policy.meal_enricher_access.arn
}

############################
# LAMBDA (zip produced at infra/envs/dev by your buildspec)
############################

# Publish the requests layer built by CodeBuild (copied to infra/envs/dev/lambda/requests-layer.zip)
resource "aws_lambda_layer_version" "requests" {
  layer_name               = "requests-layer-dev"
  filename                 = "${path.module}/lambda/requests-layer.zip"
  compatible_runtimes      = ["python3.12"]
  compatible_architectures = ["x86_64"]
  description              = "Requests lib for meal_enricher"
}

resource "aws_lambda_function" "hb_meal_enricher_dev" {
  function_name    = "hb_meal_enricher_dev"
  role             = aws_iam_role.hb_meal_enricher_dev.arn
  handler          = "meal_enricher.lambda_handler"
  runtime          = "python3.12"
  architectures    = ["x86_64"]
  filename         = "${path.module}/lambda_meal_enricher.zip"
  source_code_hash = filebase64sha256("${path.module}/lambda_meal_enricher.zip")
  publish          = true
  timeout          = 30
  memory_size      = 512
  layers           = [aws_lambda_layer_version.requests.arn]

  environment {
    variables = {
      USER_ID = "me"

      MEALS_TABLE   = aws_dynamodb_table.hb_meals_dev.name
      TOTALS_TABLE  = aws_dynamodb_table.hb_daily_totals_dev.name
      EVENTS_TABLE  = data.aws_dynamodb_table.hb_events_dev.name

      MIGRAINES_TABLE = aws_dynamodb_table.hb_migraines_dev.name
      MEDS_TABLE      = aws_dynamodb_table.hb_meds_dev.name
      FASTING_TABLE   = aws_dynamodb_table.hb_fasting_dev.name  # NEW

      NUTRITION_SECRET_NAME = "hb_nutrition_api_key_dev"
      TWILIO_SECRET_NAME    = "hb_twilio_dev"

      CALORIES_MAX = "1800"
      PROTEIN_MIN  = "190"

      FOOD_OVERRIDES_TABLE = aws_dynamodb_table.hb_food_overrides_dev.name
    }
  }

  tags = {
    app   = "health-bot"
    stack = "dev"
  }
}

############################
# OPS: CloudWatch Alarm + SNS
############################

resource "aws_sns_topic" "ops_alarms_dev" {
  name = "hb_ops_alarms_dev"
}

resource "aws_sns_topic_subscription" "ops_email" {
  topic_arn = aws_sns_topic.ops_alarms_dev.arn
  protocol  = "email"
  endpoint  = var.billing_email # uses the var declared in main.tf
}

resource "aws_cloudwatch_metric_alarm" "meal_enricher_errors" {
  alarm_name          = "hb_meal_enricher_dev-Errors>0"
  alarm_description   = "Alerts when hb_meal_enricher_dev reports any Errors in a 5 min period."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = aws_lambda_function.hb_meal_enricher_dev.function_name
  }

  alarm_actions = [aws_sns_topic.ops_alarms_dev.arn]
  ok_actions    = [aws_sns_topic.ops_alarms_dev.arn]
}

############################
# SNS SUBSCRIPTION + PERMISSION
############################

resource "aws_lambda_permission" "allow_sns_invoke_enricher" {
  statement_id  = "AllowExecutionFromSNS"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.hb_meal_enricher_dev.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = data.aws_sns_topic.hb_meal_events_dev.arn
}

resource "aws_sns_topic_subscription" "meal_events_to_enricher" {
  topic_arn = data.aws_sns_topic.hb_meal_events_dev.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.hb_meal_enricher_dev.arn
}

# Optional: export the enricher ARN
output "meal_enricher_arn" {
  value = aws_lambda_function.hb_meal_enricher_dev.arn
}
