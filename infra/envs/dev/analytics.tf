# infra/envs/dev/analytics.tf

# Reuse existing locals (project_name/env/region/raw_bucket) from other files.
# Only define a new one for the analytics bucket name.
locals {
  analytics_bucket = "${local.project_name}-analytics-${local.env}"
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
# Layout: s3://<raw_bucket>/events/dt=YYYY-MM-DD/*.json
# -----------------------------
resource "aws_glue_catalog_table" "events_raw" {
  database_name = aws_glue_catalog_database.hb.name
  name          = "events_raw"
  table_type    = "EXTERNAL_TABLE"

  parameters = {
    classification            = "json"
    projection.enabled        = "true"
    projection.dt.type        = "date"
    projection.dt.range       = "2024-01-01,NOW"
    projection.dt.format      = "yyyy-MM-dd"
    # IMPORTANT: emit literal ${dt} for Glue → use $${dt} in HCL
    storage.location.template = "s3://${local.raw_bucket}/events/dt=$${dt}/"
    has_encrypted_data        = "false"
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
    # keep nested 'parsed' as string for flexibility (we'll use json_extract in Athena)
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
