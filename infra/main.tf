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

# The corpora are NOT here. They live in GCS
# (gs://opteryx_data/benchmarks/<version>/) so the engine can reference them as
# datasets, and the instance pulls from there directly. Results stay in AWS,
# next to the run that produced them.

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
        # The read-only GCP service-account key used to pull the corpora from
        # GCS. Scoped to this one secret: the instance has no other reason to
        # reach Secrets Manager, and the key never appears in user-data or in
        # the AMI.
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = aws_secretsmanager_secret.gcp_reader.arn
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

# The value is set out of band (`aws secretsmanager put-secret-value`), never in
# terraform: a service-account key in state is a service-account key in every
# copy of that state.
resource "aws_secretsmanager_secret" "gcp_reader" {
  name        = "opteryx-bench/gcp-reader"
  description = "Read-only GCP service account for pulling benchmark corpora from GCS"
  tags        = local.tags
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
        # The per-instance 8h kill-switch. Scoped to this project's alarm name
        # so the workflow cannot touch any other alarm in the account.
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricAlarm", "cloudwatch:DeleteAlarms"]
        Resource = "arn:aws:cloudwatch:*:*:alarm:opteryx-bench-killswitch-*"
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

# The 8h kill-switch is created per-instance at launch (infra/launch.sh) rather
# than declared here: a static alarm cannot carry an InstanceId dimension for an
# instance that does not exist yet, and a wildcard dimension matches nothing.
# This topic remains for the collect job's failure notifications.
