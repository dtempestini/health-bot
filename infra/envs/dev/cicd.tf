variable "codestar_connection_arn" {
  type        = string
  description = "CodeConnections ARN to GitHub"
}
variable "github_repo" {
  type        = string
  description = "Full repo name (owner/repo), e.g., dtempestini/health-bot"
}

# Artifact bucket for CodePipeline
resource "aws_s3_bucket" "artifacts" {
  bucket        = "health-bot-dev-codepipeline-artifacts"
  force_destroy = true
}

# -----------------------------
# IAM for CodeBuild
# -----------------------------
data "aws_iam_policy_document" "cb_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["codebuild.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "codebuild_role" {
  name               = "health-bot-dev-codebuild-role"
  assume_role_policy = data.aws_iam_policy_document.cb_assume.json
}

resource "aws_iam_role_policy_attachment" "cb_admin" {
  role       = aws_iam_role.codebuild_role.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

# -----------------------------
# IAM for CodePipeline
# -----------------------------
data "aws_iam_policy_document" "cp_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["codepipeline.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "codepipeline_role" {
  name               = "health-bot-dev-codepipeline-role"
  assume_role_policy = data.aws_iam_policy_document.cp_assume.json
}

resource "aws_iam_role_policy_attachment" "cp_admin" {
  role       = aws_iam_role.codepipeline_role.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

# -----------------------------
# CodeBuild projects (plan/apply)
# -----------------------------
resource "aws_codebuild_project" "tf_plan" {
  name         = "health-bot-dev-tf-plan"
  service_role = aws_iam_role.codebuild_role.arn

  artifacts { type = "CODEPIPELINE" }

  environment {
    compute_type = "BUILD_GENERAL1_SMALL"
    image        = "aws/codebuild/standard:7.0"
    type         = "LINUX_CONTAINER"
  }

  source { type = "CODEPIPELINE" }
}

resource "aws_codebuild_project" "tf_apply" {
  name         = "health-bot-dev-tf-apply"
  service_role = aws_iam_role.codebuild_role.arn

  artifacts { type = "CODEPIPELINE" }

  environment {
    compute_type = "BUILD_GENERAL1_SMALL"
    image        = "aws/codebuild/standard:7.0"
    type         = "LINUX_CONTAINER"
  }

  source { type = "CODEPIPELINE" }
}

# -----------------------------
# CodePipeline: Source -> Plan -> Approve -> Apply
# -----------------------------
resource "aws_codepipeline" "infra_pipeline" {
  name     = "health-bot-dev-infra-pipeline"
  role_arn = aws_iam_role.codepipeline_role.arn

  artifact_store {
    location = aws_s3_bucket.artifacts.bucket
    type     = "S3"
  }

  stage {
    name = "Source"
    action {
      name             = "GitHub"
      category         = "Source"
      owner            = "AWS"
      provider         = "CodeStarSourceConnection"
      version          = "1"
      output_artifacts = ["source_output"]
      configuration = {
        ConnectionArn    = var.codestar_connection_arn
        FullRepositoryId = var.github_repo
        BranchName       = "main"
      }
    }
  }

  stage {
    name = "Plan"
    action {
      name             = "TerraformPlan"
      category         = "Build"
      owner            = "AWS"
      provider         = "CodeBuild"
      input_artifacts  = ["source_output"]
      output_artifacts = ["plan_output"]
      version          = "1"
      configuration = {
        ProjectName       = aws_codebuild_project.tf_plan.name
        BuildspecOverride = "infra/envs/dev/buildspecs/terraform-plan.yml"
        PrimarySource     = "source_output"
      }
    }
  }

/* comment out approval

  stage {
    name = "Approve"
    action {
      name     = "ManualApproval"
      category = "Approval"
      owner    = "AWS"
      provider = "Manual"
      version  = "1"
    }
  }
*/

  stage {
    name = "Apply"
    action {
      name            = "TerraformApply"
      category        = "Build"
      owner           = "AWS"
      provider        = "CodeBuild"
      input_artifacts = ["source_output", "plan_output"]
      version         = "1"
      configuration = {
        ProjectName       = aws_codebuild_project.tf_apply.name
        BuildspecOverride = "infra/envs/dev/buildspecs/terraform-apply.yml"
        PrimarySource     = "source_output"
      }
    }
  }
}
