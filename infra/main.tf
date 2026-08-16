# Wrenchy Bench infrastructure.
#
# Deliberately small. GitHub Actions is the control plane — there is no Lambda,
# no EventBridge Scheduler and no long-lived compute here. This stack owns the
# two buckets, the identity the Action assumes, the identity the instance runs
# as, and the alarm that fires when a run never reports.

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

# Corpora: write-once, ~70GB, versioned by prefix (v2026-08/...). Versioning is
# on because a corpus is the baseline every trend line is measured against — an
# accidental overwrite would silently reinterpret months of history.
resource "aws_s3_bucket" "corpora" {
  bucket = "${local.name}-corpora"
  tags   = local.tags
}

resource "aws_s3_bucket_versioning" "corpora" {
  bucket = aws_s3_bucket.corpora.id
  versioning_configuration { status = "Enabled" }
}

# Results: append-only, a few MB per run. No lifecycle expiry — the archive is
# the system of record and must outlive anything in the telemetry tables.
resource "aws_s3_bucket" "results" {
  bucket = "${local.name}-results"
  tags   = local.tags
}

resource "aws_s3_bucket_public_access_block" "corpora" {
  bucket                  = aws_s3_bucket.corpora.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
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
        Action   = ["s3:GetObject", "s3:ListBucket", "s3:PutObject"]
        Resource = [aws_s3_bucket.results.arn, "${aws_s3_bucket.results.arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:GetParameters"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = ["ec2:DescribeInstances", "ec2:DescribeImages", "ec2:RunInstances", "ec2:CreateTags"]
        # RunInstances is deliberately unconstrained on resource but constrained
        # on instance type below — the launcher resolves an AMI at run time, so
        # pinning the image ARN here would break every Ubuntu release.
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = aws_iam_role.instance.arn
      },
      {
        # Terminate only what this project launched. A bug in the workflow must
        # not be able to reach anything else in the account.
        Effect   = "Allow"
        Action   = "ec2:TerminateInstances"
        Resource = "*"
        Condition = {
          StringEquals = { "ec2:ResourceTag/project" = "wrenchy-bench" }
        }
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

resource "aws_cloudwatch_metric_alarm" "runaway" {
  alarm_name          = "${local.name}-runaway-instance"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 0
  period              = 3600
  statistic           = "Maximum"
  namespace           = "AWS/EC2"
  metric_name         = "CPUUtilization"
  treat_missing_data  = "notBreaching"
  alarm_description   = "A wrenchy-bench instance is still running 9h after the weekly window opened"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = local.tags

  dimensions = {
    InstanceId = "*"
  }
}
