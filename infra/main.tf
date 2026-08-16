# Wrenchy Bench infrastructure.
#
# The trigger lives in AWS. EventBridge Scheduler fires a Lambda weekly, the
# Lambda launches the instance, the instance runs the suite and writes its
# bundle to S3. Nothing outside this account can start an EC2 instance in it:
# no principal outside the account holds ec2:RunInstances, and the one external
# identity that exists (GitHub, below) is read-only.
#
# GitHub still COLLECTS — a scheduled workflow reads the finished bundle from
# S3, compares it against history, and publishes the site. That half needs only
# read access, and keeping it in Actions means a bad comparison can be re-run
# without paying for another four hours of EC2.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

data "aws_caller_identity" "current" {}

locals {
  name = "opteryx-bench"
  tags = {
    project   = "wrenchy-bench"
    managedby = "terraform"
  }
}

# ---------------------------------------------------------------------------
# Buckets
# ---------------------------------------------------------------------------

# Corpora: write-once, ~76GB, versioned by prefix (v2026-08/...). In-region so
# the runner's transfer is free. Versioning is on because a corpus is the
# baseline every trend line is measured against — an accidental overwrite would
# silently reinterpret months of history.
#
# The same bytes also live in GCS for engine reference (see harness/config.py);
# that copy is written by the publisher, never read here.
resource "aws_s3_bucket" "corpora" {
  bucket = "${local.name}-corpora"
  tags   = local.tags
}

resource "aws_s3_bucket_versioning" "corpora" {
  bucket = aws_s3_bucket.corpora.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_public_access_block" "corpora" {
  bucket                  = aws_s3_bucket.corpora.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Results: append-only, a few MB per run. No lifecycle expiry — the archive is
# the system of record and must outlive anything in the telemetry tables.
resource "aws_s3_bucket" "results" {
  bucket = "${local.name}-results"
  tags   = local.tags
}

resource "aws_s3_bucket_public_access_block" "results" {
  bucket                  = aws_s3_bucket.results.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ---------------------------------------------------------------------------
# The instance's identity: read corpora, write its own run prefix. Nothing else.
# ---------------------------------------------------------------------------

resource "aws_iam_role" "instance" {
  name = "${local.name}-instance"
  tags = local.tags
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "instance" {
  name = "${local.name}-instance"
  role = aws_iam_role.instance.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Resource = [aws_s3_bucket.corpora.arn, "${aws_s3_bucket.corpora.arn}/*"]
      },
      {
        # Write-only into results, and no delete: a run cannot erase an earlier
        # run's bundle, whatever goes wrong on the box.
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:ListBucket"]
        Resource = [aws_s3_bucket.results.arn, "${aws_s3_bucket.results.arn}/*"]
      }
    ]
  })
}

resource "aws_iam_instance_profile" "instance" {
  name = "${local.name}-instance"
  role = aws_iam_role.instance.name
}

# ---------------------------------------------------------------------------
# GitHub Actions identity, via OIDC. No long-lived access keys anywhere.
# ---------------------------------------------------------------------------

# The collector's identity. It can read finished bundles and the platform
# credential, and nothing else — no ec2:*, no iam:PassRole. Compare with the
# previous version of this file, where the same role could launch instances.
resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

resource "aws_iam_role" "actions" {
  name = "${local.name}-actions"
  tags = local.tags
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        # Scoped to this repository. Without the sub condition any repo in any
        # org could assume this role and launch instances on the account.
        StringLike = {
          "token.actions.githubusercontent.com:sub" = "repo:${var.github_repo}:*"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "actions" {
  name = "${local.name}-actions"
  role = aws_iam_role.actions.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Resource = [aws_s3_bucket.results.arn, "${aws_s3_bucket.results.arn}/*"]
      },
      {
        # The platform credential used to commit results into
        # opteryx.telemetry.*. Read from Secrets Manager rather than duplicated
        # as a GitHub secret so the PAT has one home and one rotation.
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = var.opteryx_pat_secret_arn
      },
      {
        # Read-only: find-run.sh maps a run id back to its instance so the
        # kill-switch alarm can be named. No mutation of any kind.
        Effect   = "Allow"
        Action   = "ec2:DescribeInstances"
        Resource = "*"
      },
      {
        # Tidy up the kill-switch alarm after a run reports. Deleting an alarm
        # is the only mutation the collector can make in the account.
        Effect   = "Allow"
        Action   = "cloudwatch:DeleteAlarms"
        Resource = "arn:aws:cloudwatch:*:*:alarm:opteryx-bench-killswitch-*"
      }
    ]
  })
}

# ---------------------------------------------------------------------------
# Network. Egress only — the instance pulls from S3, GitHub and PyPI and is
# never connected to. No SSH: there is nothing to log into, and a key pair
# would be one more thing to rotate.
# ---------------------------------------------------------------------------

resource "aws_security_group" "instance" {
  name        = "${local.name}-instance"
  description = "Wrenchy Bench runner: egress only"
  vpc_id      = var.vpc_id
  tags        = local.tags

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ---------------------------------------------------------------------------
# The launcher reads these rather than hardcoding, so a re-apply that moves the
# subnet cannot silently launch into the wrong VPC.
# ---------------------------------------------------------------------------

resource "aws_ssm_parameter" "subnet_id" {
  name  = "/${local.name}/subnet-id"
  type  = "String"
  value = var.subnet_id
  tags  = local.tags
}

resource "aws_ssm_parameter" "security_group_id" {
  name  = "/${local.name}/security-group-id"
  type  = "String"
  value = aws_security_group.instance.id
  tags  = local.tags
}

resource "aws_ssm_parameter" "instance_profile_arn" {
  name  = "/${local.name}/instance-profile-arn"
  type  = "String"
  value = aws_iam_instance_profile.instance.arn
  tags  = local.tags
}

# ---------------------------------------------------------------------------
# Cost guard. The instance self-terminates and carries a 7h shutdown watchdog;
# this catches the case where it died before reaching either — an instance
# still running well past the longest plausible run.
# ---------------------------------------------------------------------------

resource "aws_sns_topic" "alerts" {
  name = "${local.name}-alerts"
  tags = local.tags
}

# The 8h kill-switch is created per-instance at launch (infra/launch.sh) rather
# than declared here: a static alarm cannot carry an InstanceId dimension for an
# instance that does not exist yet, and a wildcard dimension matches nothing.
# This topic remains for the collect job's failure notifications.


# ---------------------------------------------------------------------------
# The trigger: EventBridge Scheduler -> Lambda -> RunInstances.
#
# The bootstrap script is uploaded to S3 and read by the Lambda at launch,
# rather than baked into the function. The script is data, not code: changing
# what the box runs is an object upload, not a Lambda deployment, and the
# version that ran is recoverable from the bucket.
# ---------------------------------------------------------------------------

resource "aws_s3_object" "bootstrap" {
  bucket = aws_s3_bucket.results.id
  key    = "bootstrap/user-data.sh"
  source = "${path.module}/../bootstrap/user-data.sh"
  etag   = filemd5("${path.module}/../bootstrap/user-data.sh")
}

data "archive_file" "launcher" {
  type        = "zip"
  source_file = "${path.module}/lambda/launch.py"
  output_path = "${path.module}/.build/launch.zip"
}

resource "aws_iam_role" "launcher" {
  name = "${local.name}-launcher"
  tags = local.tags
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "launcher" {
  name = "${local.name}-launcher"
  role = aws_iam_role.launcher.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Effect   = "Allow"
        Action   = ["ec2:RunInstances", "ec2:CreateTags", "ec2:DescribeImages"]
        Resource = "*"
      },
      {
        # The launcher can hand the instance its role and nothing else.
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = aws_iam_role.instance.arn
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:GetParameters"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "${aws_s3_bucket.results.arn}/bootstrap/*"
      },
      {
        Effect   = "Allow"
        Action   = "cloudwatch:PutMetricAlarm"
        Resource = "arn:aws:cloudwatch:*:*:alarm:opteryx-bench-killswitch-*"
      },
      {
        Effect   = "Allow"
        Action   = "sns:Publish"
        Resource = aws_sns_topic.alerts.arn
      }
    ]
  })
}

resource "aws_lambda_function" "launcher" {
  function_name    = "${local.name}-launcher"
  role             = aws_iam_role.launcher.arn
  handler          = "launch.handler"
  runtime          = "python3.13"
  filename         = data.archive_file.launcher.output_path
  source_code_hash = data.archive_file.launcher.output_base64sha256
  timeout          = 60
  tags             = local.tags

  environment {
    variables = {
      BOOTSTRAP_BUCKET = aws_s3_bucket.results.id
      BOOTSTRAP_KEY    = aws_s3_object.bootstrap.key
      CORPUS_PREFIX    = "s3://${aws_s3_bucket.corpora.id}/${var.corpus_version}"
      RESULTS_BUCKET   = "s3://${aws_s3_bucket.results.id}"
      ALERTS_TOPIC     = aws_sns_topic.alerts.arn
      INSTANCE_TYPE    = var.instance_type
      ENGINE_VERSION   = var.engine_version
      HARNESS_REF      = var.harness_ref
    }
  }
}

resource "aws_scheduler_schedule" "weekly" {
  name                         = "${local.name}-weekly"
  schedule_expression          = "cron(0 2 ? * SUN *)"
  schedule_expression_timezone = "UTC"
  # No catch-up. A run that missed its window is a run against a different
  # engine than the one it was scheduled for, and a benchmark that fires late
  # in a burst is worse than one that skips a week.
  flexible_time_window { mode = "OFF" }

  target {
    arn      = aws_lambda_function.launcher.arn
    role_arn = aws_iam_role.scheduler.arn
    retry_policy {
      maximum_retry_attempts = 0
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name = "${local.name}-scheduler"
  tags = local.tags
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "scheduler.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  name = "${local.name}-scheduler"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.launcher.arn
    }]
  })
}
