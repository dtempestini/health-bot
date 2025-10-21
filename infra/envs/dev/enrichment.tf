##
## PHASE 2: Meal Enricher + Daily Totals + WhatsApp replies
## Tables: hb_meals_dev, hb_daily_totals_dev
## Lambda: hb_meal_enricher_dev
## SNS sub: hb_meal_events_dev -> hb_meal_enricher_dev
##

############################
# DYNAMODB TABLES
############################

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

# Daily totals (current pattern: pk="total#me", sk="YYYY-MM-DD")
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

############################
# LOOKUPS FOR EXISTING RESOURCES
############################

# SNS created in ingest.tf
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

# Reuse the assume-role policy declared in ingest.tf
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
    sid     = "MealsRW"
    actions = ["dynamodb:PutItem","dynamodb:GetItem","dynamodb:DescribeTable"]
    resources = [aws_dynamodb_table.hb_meals_dev.arn]
  }

  statement {
    sid     = "TotalsRW"
    actions = ["dynamodb:UpdateItem","dynamodb:GetItem","dynamodb:DescribeTable"]
    resources = [aws_dynamodb_table.hb_daily_totals_dev.arn]
  }

  statement {
    sid     = "EventsWrite"
    actions = ["dynamodb:PutItem"]
    resources = [data.aws_dynamodb_table.hb_events_dev.arn]
  }

  statement {
    sid     = "SecretsRead"
    actions = ["secretsmanager:GetSecretValue"]
    resources = ["*"]
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

# Publish the requests layer built by CodeBuild
resource "aws_lambda_layer_version" "requests" {
  layer_name               = "requests-layer-dev"
  filename                 = "${path.module}/lambda/requests-layer.zip"
  compatible_runtimes      = ["python3.12"]
  compatible_architectures = ["x86_64"] # keep in sync with your function arch
  description              = "Requests lib for meal_enricher"
}

resource "aws_lambda_function" "hb_meal_enricher_dev" {
  function_name    = "hb_meal_enricher_dev"
  role             = aws_iam_role.hb_meal_enricher_dev.arn
  handler          = "meal_enricher.lambda_handler"
  runtime          = "python3.12"
  architectures    = ["x86_64"] # matches the CodeBuild layer arch
  filename         = "${path.module}/lambda_meal_enricher.zip"
  source_code_hash = filebase64sha256("${path.module}/lambda_meal_enricher.zip")
  publish          = true
  timeout          = 30
  memory_size      = 512

  environment {
    variables = {
      USER_ID               = "me"

      MEALS_TABLE           = aws_dynamodb_table.hb_meals_dev.name
      TOTALS_TABLE          = aws_dynamodb_table.hb_daily_totals_dev.name
      EVENTS_TABLE          = data.aws_dynamodb_table.hb_events_dev.name

      NUTRITION_SECRET_NAME = "hb_nutrition_api_key_dev"
      TWILIO_SECRET_NAME    = "hb_twilio_dev"

      CALORIES_MAX          = "1800"
      PROTEIN_MIN           = "210"
    }
  }

  tags = {
    app   = "health-bot"
    stack = "dev"
  }

    layers = [aws_lambda_layer_version.requests.arn]
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
