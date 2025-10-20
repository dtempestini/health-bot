locals {
  curated_bucket         = "${local.project_name}-curated-${local.env}"
  meals_table            = "hb_meals_${local.env}"
  totals_table           = "hb_daily_totals_${local.env}"
  meal_enricher_name     = "hb_meal_enricher_${local.env}"
  notifications_topic    = "hb_notifications_${local.env}"
  nutrition_secret_name  = "hb_nutrition_api_key_${local.env}"
}

# ------------- S3 (curated outputs) -------------
resource "aws_s3_bucket" "curated" {
  bucket = local.curated_bucket
}

resource "aws_s3_bucket_versioning" "curated_v" {
  bucket = aws_s3_bucket.curated.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "curated_sse" {
  bucket = aws_s3_bucket.curated.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# ------------- DynamoDB: meals (per ingested meal) -------------
resource "aws_dynamodb_table" "meals" {
  name         = local.meals_table
  billing_mode = "PAY_PER_REQUEST"

  hash_key  = "pk"
  range_key = "sk"

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

# ------------- DynamoDB: daily totals (one row per day) -------------
resource "aws_dynamodb_table" "totals" {
  name         = local.totals_table
  billing_mode = "PAY_PER_REQUEST"

  hash_key  = "pk"
  range_key = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  tags = {
    Project = local.project_name
    Env     = local.env
  }
}

# ------------- SNS notifications -------------
resource "aws_sns_topic" "notifications" {
  name = local.notifications_topic
}

resource "aws_sns_topic_subscription" "email_me" {
  topic_arn = aws_sns_topic.notifications.arn
  protocol  = "email"
  endpoint  = var.billing_email
}

# ------------- Secrets Manager (Nutritionix credentials) -------------
# If you already created this secret by hand with the same name, keep this resource
# and later import it; or comment it out. Otherwise Terraform will create an empty secret
# that you will fill in the console.
data "aws_secretsmanager_secret" "nutrition_api_key" {
  name        = local.nutrition_secret_name
}

data "aws_secretsmanager_secret" "twilio" {
  name = "hb_twilio_${local.env}"
}


# ------------- IAM for meal_enricher -------------
data "aws_iam_policy_document" "meal_lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "meal_role" {
  name               = "${local.meal_enricher_name}-role"
  assume_role_policy = data.aws_iam_policy_document.meal_lambda_assume.json
}

data "aws_iam_policy_document" "meal_policy" {

  statement {
    sid     = "WriteMealsAndTotals"
    actions = [
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:GetItem"
    ]
    resources = [
      aws_dynamodb_table.meals.arn,
      aws_dynamodb_table.totals.arn,
      aws_dynamodb_table.events.arn
    ]
  }

  statement {
    sid     = "WriteCuratedS3"
    actions = ["s3:PutObject", "s3:PutObjectAcl"]
    resources = ["${aws_s3_bucket.curated.arn}/*"]
  }

  statement {
    sid     = "PublishNotifications"
    actions = ["sns:Publish"]
    resources = [aws_sns_topic.notifications.arn]
  }

  statement {
    sid     = "ReadSecret"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [data.aws_secretsmanager_secret.nutrition_api_key.arn]
  }

  statement {
  sid     = "ReadTwilioSecret"
  actions = ["secretsmanager:GetSecretValue"]
  resources = [data.aws_secretsmanager_secret.twilio.arn]
}


  statement {
    sid     = "Logs"
    actions = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:*:*:*"]
  }
}

resource "aws_iam_policy" "meal_inline" {
  name   = "${local.meal_enricher_name}-policy"
  policy = data.aws_iam_policy_document.meal_policy.json
}

resource "aws_iam_role_policy_attachment" "meal_attach" {
  role       = aws_iam_role.meal_role.name
  policy_arn = aws_iam_policy.meal_inline.arn
}

# ---------- Lambda Layer for Python requests ----------
resource "aws_lambda_layer_version" "requests" {
  filename   = "${path.module}/lambda/requests-layer.zip"
  layer_name = "requests"
  compatible_runtimes = ["python3.12"]
}

# ------------- Lambda: meal_enricher -------------
resource "aws_lambda_function" "meal_enricher" {
  function_name = local.meal_enricher_name
  role          = aws_iam_role.meal_role.arn
  runtime       = "python3.12"
handler        = "meal_enricher.lambda_handler"
filename       = "${path.module}/lambda_meal_enricher.zip"
  source_code_hash = filebase64sha256("${path.module}/lambda_meal_enricher.zip")
  timeout       = 20

    layers = [aws_lambda_layer_version.requests.arn]

  environment {
    variables = {
      USER_ID               = "me"
      MEALS_TABLE           = aws_dynamodb_table.meals.name
      TOTALS_TABLE          = aws_dynamodb_table.totals.name
      CURATED_BUCKET        = aws_s3_bucket.curated.bucket
      NOTIFY_TOPIC          = aws_sns_topic.notifications.arn
      NUTRITION_SECRET_NAME = data.aws_secretsmanager_secret.nutrition_api_key.name
      EVENTS_TABLE           = aws_dynamodb_table.events.name
      TWILIO_SECRET_NAME    = data.aws_secretsmanager_secret.twilio.name
    }
  }
}

# NOTE: Avoid name collision with any existing output named 'sns_topic_arn'
output "sns_notifications_topic_arn" {
  value = aws_sns_topic.notifications.arn
}

# ===========================================================
# SNS Topic for meal events
# ===========================================================

resource "aws_sns_topic" "meal_events" {
  name = "hb_meal_events_dev"
}

# Allow SNS to invoke the enricher Lambda
resource "aws_lambda_permission" "allow_sns_to_invoke_enricher" {
  statement_id  = "AllowInvokeFromSNS"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.meal_enricher.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.meal_events.arn
}

# Subscribe the enricher Lambda to the SNS topic
resource "aws_sns_topic_subscription" "meal_enricher_sub" {
  topic_arn = aws_sns_topic.meal_events.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.meal_enricher.arn
}

output "meal_events_topic_arn" {
  value = aws_sns_topic.meal_events.arn
}
