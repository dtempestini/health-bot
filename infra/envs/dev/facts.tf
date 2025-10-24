##
## PHASE: Migraine facts ingest + on-demand/automated sending
##

locals {
  facts_bucket = "hb-facts-dev"
  facts_prefix = "migraine/"
}

############################
# S3 bucket for CSV drops
############################
resource "aws_s3_bucket" "facts" {
  bucket = local.facts_bucket
  force_destroy = true

  tags = { app = "health-bot", stack = "dev", part = "facts" }
}

resource "aws_s3_bucket_versioning" "facts" {
  bucket = aws_s3_bucket.facts.id
  versioning_configuration {
    status = "Enabled"
  }
}

############################
# DynamoDB facts table
############################
resource "aws_dynamodb_table" "hb_migraine_facts_dev" {
  name         = "hb_migraine_facts_dev"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk" # user id ("me")
    type = "S"
  }

  attribute {
    name = "sk" # "fact#<id>"
    type = "S"
  }

  attribute {
    name = "dt" # YYYY-MM-DD (ingested or last sent)
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  global_secondary_index {
    name               = "gsi_dt"
    hash_key           = "dt"
    range_key          = "sk"
    projection_type    = "ALL"
  }

  lifecycle {
    prevent_destroy = true
  }

  tags = {
    app   = "health-bot"
    stack = "dev"
    part  = "facts"
  }
}

############################
# Optional SQS queue for replay/refresh
############################
resource "aws_sqs_queue" "hb_fact_queue_dev" {
  name                        = "hb_fact_queue_dev"
  message_retention_seconds   = 1209600
  visibility_timeout_seconds  = 120
  receive_wait_time_seconds   = 10
  tags = { app = "health-bot", stack = "dev", part = "facts" }
}

############################
# Lambda: S3 ingest (CSV -> DDB [+ SQS])
############################
data "aws_iam_policy_document" "facts_ingest_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "facts_ingest" {
  name               = "hb-facts-ingest-dev"
  assume_role_policy = data.aws_iam_policy_document.facts_ingest_assume.json
  tags = { app = "health-bot", stack = "dev" }
}

resource "aws_iam_role_policy_attachment" "facts_ingest_logs" {
  role       = aws_iam_role.facts_ingest.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "facts_ingest_access" {
  statement {
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.facts.arn}/*"]
  }
  statement {
    actions   = ["dynamodb:PutItem", "dynamodb:BatchWriteItem", "dynamodb:DescribeTable"]
    resources = [aws_dynamodb_table.hb_migraine_facts_dev.arn]
  }
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.hb_fact_queue_dev.arn]
  }
}

resource "aws_iam_policy" "facts_ingest_access" {
  name   = "hb-facts-ingest-access-dev"
  policy = data.aws_iam_policy_document.facts_ingest_access.json
}

resource "aws_iam_role_policy_attachment" "facts_ingest_attach" {
  role       = aws_iam_role.facts_ingest.name
  policy_arn = aws_iam_policy.facts_ingest_access.arn
}

resource "aws_lambda_function" "facts_ingest" {
  function_name    = "hb_facts_ingest_dev"
  role             = aws_iam_role.facts_ingest.arn
  handler          = "facts_ingest.lambda_handler"
  runtime          = "python3.12"
  architectures    = ["x86_64"]
  filename         = "${path.module}/lambda_facts_ingest.zip"
  source_code_hash = filebase64sha256("${path.module}/lambda_facts_ingest.zip")
  publish          = true
  timeout          = 60
  memory_size      = 256
  environment {
    variables = {
      FACTS_TABLE   = aws_dynamodb_table.hb_migraine_facts_dev.name
      SQS_URL       = aws_sqs_queue.hb_fact_queue_dev.id
      USER_ID       = "me"
    }
  }
  tags = { app = "health-bot", stack = "dev" }
}

resource "aws_s3_bucket_notification" "facts_notify" {
  bucket = aws_s3_bucket.facts.id
  lambda_function {
    lambda_function_arn = aws_lambda_function.facts_ingest.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = local.facts_prefix
    filter_suffix       = ".csv"
  }
  depends_on = [aws_lambda_permission.allow_s3_facts_ingest]
}

resource "aws_lambda_permission" "allow_s3_facts_ingest" {
  statement_id  = "AllowS3InvokeFactsIngest"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.facts_ingest.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = aws_s3_bucket.facts.arn
}

############################
# Lambda: facts sender (hourly tick + SQS consumers)
############################
data "aws_iam_policy_document" "facts_sender_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "facts_sender" {
  name               = "hb-facts-sender-dev"
  assume_role_policy = data.aws_iam_policy_document.facts_sender_assume.json
  tags = { app = "health-bot", stack = "dev" }
}

resource "aws_iam_role_policy_attachment" "facts_sender_logs" {
  role       = aws_iam_role.facts_sender.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "facts_sender_access" {
  statement {
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:Scan", "dynamodb:DescribeTable"]
    resources = [aws_dynamodb_table.hb_migraine_facts_dev.arn]
  }
  statement {
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.hb_fact_queue_dev.arn]
  }
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "facts_sender_access" {
  name   = "hb-facts-sender-access-dev"
  policy = data.aws_iam_policy_document.facts_sender_access.json
}

resource "aws_iam_role_policy_attachment" "facts_sender_attach" {
  role       = aws_iam_role.facts_sender.name
  policy_arn = aws_iam_policy.facts_sender_access.arn
}

resource "aws_lambda_function" "facts_sender" {
  function_name    = "hb_facts_sender_dev"
  role             = aws_iam_role.facts_sender.arn
  handler          = "facts_sender.lambda_handler"
  runtime          = "python3.12"
  architectures    = ["x86_64"]
  filename         = "${path.module}/lambda_facts_sender.zip"
  source_code_hash = filebase64sha256("${path.module}/lambda_facts_sender.zip")
  publish          = true
  timeout          = 60
  memory_size      = 256
  environment {
    variables = {
      FACTS_TABLE          = aws_dynamodb_table.hb_migraine_facts_dev.name
      SQS_URL              = aws_sqs_queue.hb_fact_queue_dev.id
      TWILIO_SECRET_NAME   = "hb_twilio_dev"
      USER_ID              = "me"
      DEFAULT_DAILY_HOUR   = "9"         # 9am ET default
      TZ_NAME              = "America/New_York"
    }
  }
  tags = { app = "health-bot", stack = "dev" }
}

# EventBridge hourly tick
resource "aws_cloudwatch_event_rule" "facts_hourly" {
  name                = "hb-facts-hourly-dev"
  schedule_expression = "rate(1 hour)"
  tags = { app = "health-bot", stack = "dev" }
}

resource "aws_cloudwatch_event_target" "facts_hourly_target" {
  rule      = aws_cloudwatch_event_rule.facts_hourly.name
  target_id = "facts-sender"
  arn       = aws_lambda_function.facts_sender.arn
}

resource "aws_lambda_permission" "allow_events_facts_sender" {
  statement_id  = "AllowEventsInvokeFactsSender"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.facts_sender.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.facts_hourly.arn
}

# SQS event source mapping (optional consumer)
resource "aws_lambda_event_source_mapping" "facts_sqs_source" {
  event_source_arn  = aws_sqs_queue.hb_fact_queue_dev.arn
  function_name     = aws_lambda_function.facts_sender.arn
  batch_size        = 5
  enabled           = true
}

# Outputs (handy)
output "facts_bucket"   { value = aws_s3_bucket.facts.bucket }
output "facts_prefix"   { value = local.facts_prefix }
output "facts_table"    { value = aws_dynamodb_table.hb_migraine_facts_dev.name }
output "facts_queue"    { value = aws_sqs_queue.hb_fact_queue_dev.id }
