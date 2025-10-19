locals {
  project_name      = "health-bot"
  env               = "dev"
  region            = "us-east-1"

  raw_bucket        = "${local.project_name}-raw-${local.env}"         # already created earlier
  analytics_bucket  = "${local.project_name}-analytics-${local.env}"    # new, for Athena results
}

# -----------------------------
# S3 bucket for Athena results
# -----------------------------
resource "aws_s3_bucket" "analytics" {
  bucket = local.analytics_bucket
}

resource "aws_s3_bucket_versioning" "analytics_v" {
  bucket = aws_s3_bucket.analytics.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "analytics_sse" {
  bucket = aws_s3_bucket.analytics.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# -----------------------------
# Glue Database
# -----------------------------
resource "aws_glue_catalog_database" "hb" {
  name = "hb_${local.env}"
}

# -----------------------------
# Glue Table over raw events (JSON) with partition projection on dt
# S3 layout: s3://<raw_bucket>/events/dt=YYYY-MM-DD/*.json
# -----------------------------
resource "aws_glue_catalog_table" "events_raw" {
  database_name = aws_glue_catalog_database.hb.name
  name          = "events_raw"
  table_type    = "EXTERNAL_TABLE"

  parameters = {
    "classification"                = "json"
    "projection.enabled"            = "true"
    "projection.dt.type"            = "date"
    "projection.dt.range"           = "2024-01-01,NOW"
    "projection.dt.format"          = "yyyy-MM-dd"
    "storage.location.template"     = "s3://${local.raw_bucket}/events/dt=\${dt}/"
    "has_encrypted_data"            = "false"
  }

  partition_keys {
    name = "dt"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${local.raw_bucket}/events/"
    input_format  = "org.apache.hadoop.mapred.TextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

    ser_de_info {
      name                  = "OpenXJSONSerDe"
      serialization_library = "org.openx.data.jsonserde.JsonSerDe"
    }

    # Minimal top-level columns present in your JSON
    columns {
      name = "id"
      type = "string"
    }
    columns {
      name = "user_id"
      type = "string"
    }
    columns {
      name = "ts"
      type = "string"
    }
    columns {
      name = "type"
      type = "string"
    }
    columns {
      name = "text"
      type = "string"
    }
    columns {
      name = "source"
      type = "string"
    }
    # Keep nested 'parsed' as a string for now (we'll parse with JSON functions)
    columns {
      name = "parsed"
      type = "string"
    }
  }
}

# -----------------------------
# Athena Workgroup (results -> analytics bucket)
# -----------------------------
resource "aws_athena_workgroup" "wg" {
  name = "hb_${local.env}_wg"

  configuration {
    enforce_workgroup_configuration = true

    result_configuration {
      output_location = "s3://${aws_s3_bucket.analytics.bucket}/athena-results/"
    }
  }

  description = "Health-bot ${local.env} workgroup"
  state       = "ENABLED"
}
