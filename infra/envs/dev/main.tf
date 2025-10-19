terraform {
  backend "s3" {
    bucket         = "health-bot-tfstate" # <-- your bootstrap bucket
    key            = "infra/dev/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "health-bot-tf-locks" # <-- your bootstrap lock table
    encrypt        = true
  }
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" { region = "us-east-1" }

variable "billing_email" {
  type        = string
  description = "Email to receive billing alerts"
}

resource "aws_sns_topic" "billing_alerts" {
  name = "health-bot-dev-billing-alerts"
}

resource "aws_sns_topic_subscription" "billing_email" {
  topic_arn = aws_sns_topic.billing_alerts.arn
  protocol  = "email"
  endpoint  = var.billing_email
}

resource "aws_budgets_budget" "monthly_cost" {
  name         = "HealthBot Dev Monthly"
  budget_type  = "COST"
  time_unit    = "MONTHLY"
  limit_amount = "20"
  limit_unit   = "USD"

  # Soft alert at 50% ($10)
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 50
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_sns_topic_arns  = [aws_sns_topic.billing_alerts.arn]
    subscriber_email_addresses = [var.billing_email]
  }

  # Hard alert at 100% ($20)
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_sns_topic_arns  = [aws_sns_topic.billing_alerts.arn]
    subscriber_email_addresses = [var.billing_email]
  }
}

output "sns_topic_arn" { value = aws_sns_topic.billing_alerts.arn }
