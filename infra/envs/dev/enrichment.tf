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
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "curated_sse" {
  bucket = aws_s3_bucket.curated.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

# ------------- DynamoDB: meals (per ingested meal) -------------
resource "aws_dynamodb_table" "meals" {
  name         = local.meals_table
  billing_mode = "PAY_PER_REQUEST"

  hash_key  = "pk"         # user id
  range_key = "sk"         # ISO timestamp

  attribute { name = "pk"   type = "S" }
  attribute { name = "sk"   type = "S" }
  attribute { name = "dt"   type = "S" }
  attribute { name = "type" type = "S" }

  global_secondary_index {
    name            = "gsi_dt"
    hash_key        = "dt"
    range_key       = "type"
    projection_type = "ALL"
  }

  tags = { Project = local.project_name, Env = local.env }
}

# ------------- DynamoDB: daily totals (one row per day) -------------
resource "aws_dynamodb_table" "totals" {
  name         = local.totals_table
  billing_mode = "PAY_PER_REQUEST"

  hash_key  = "pk"     # user id
  range_key = "sk"     # date YYYY-MM-DD

  attribute { name = "pk" type = "S" }
  attribute { name = "sk" type = "S" }

  tags = { Project = local.project_name, Env = local.env }
}

# ------------- SNS notifications -------------
resource "aws_sns_topic" "notifications" {
  name = local.notifications_topic
}

# subscribe your billing email (confirm email)
resource "aws_sns_topic_subscription" "email_me" {
  topic_arn = aws_sns_topic.notifications.arn
  protocol  = "email"
  endpoint  = var.billing_email
}

# ------------- Secrets Manager (Nutritionix credentials) -------------
# If you already created the secret in the console with this exact name,
# keep this resource and import it; or comment this out. Otherwise Terraform will create it.
resource "aws_secretsmanager_secret" "nutrition_api_key" {
  name        = local.nutrition_secret_name
  description = "Nutritionix App ID/Key (JSON: {\"app_id\":\"...\",\"app_key\":\"...\"})"
}

# ------------- IAM for meal_enricher -------------
data "aws_iam_policy_document" "meal_lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals { type = "Service" identifiers = ["lambda.amazonaws.com"] }
  }
}

resource "aws_iam_role" "meal_role" {
  name               = "${local.meal_enricher_name}-role"
  assume_role_policy = data.aws_iam_policy_document.meal_lambda_assume.json
}

data "aws_iam_policy_document" "meal_policy" {
  statement {
    sid     = "ReadEventsStream"
    actions = ["dynamodb:DescribeStream","dynamodb:GetRecords","dynamodb:GetShardIterator","dynamodb:ListStreams"]
    resources = [aws_dynamodb_table.events.stream_arn]
  }
  statement {
    sid     = "WriteMealsAndTotals"
    actions = ["dynamodb:PutItem","dynamodb:UpdateItem","dynamodb:GetItem"]
    resources = [
      aws_dynamodb_table.meals.arn,
      aws_dynamodb_table.totals.arn
    ]
  }
  statement {
    sid     = "WriteCuratedS3"
    actions = ["s3:PutObject","s3:PutObjectAcl"]
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
    resources = [aws_secretsmanager_secret.nutrition_api_key.arn]
  }
  statement {
    sid     = "Logs"
    actions = ["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"]
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

# ------------- Lambda: meal_enricher -------------
resource "aws_lambda_function" "meal_enricher" {
  function_name = local.meal_enricher_name
  role          = aws_iam_role.meal_role.arn
  runtime       = "python3.12"
  handler       = "meal_enricher.handler"
  filename      = "${path.module}/lambda_meal_enricher.zip"
  timeout       = 20

  environment {
    variables = {
      USER_ID              = "me"
      MEALS_TABLE          = aws_dynamodb_table.meals.name
      TOTALS_TABLE         = aws_dynamodb_table.totals.name
      CURATED_BUCKET       = aws_s3_bucket.curated.bucket
      NOTIFY_TOPIC         = aws_sns_topic.notifications.arn
      NUTRITION_SECRET_NAME = aws_secretsmanager_secret.nutrition_api_key.name
    }
  }
}

# ------------- Events stream -> Lambda mapping -------------
resource "aws_lambda_event_source_mapping" "events_stream" {
  event_source_arn  = aws_dynamodb_table.events.stream_arn
  function_name     = aws_lambda_function.meal_enricher.arn
  starting_position = "LATEST"
  batch_size        = 100
  maximum_batching_window_in_seconds = 1
}

output "sns_topic_arn" { value = aws_sns_topic.notifications.arn }
