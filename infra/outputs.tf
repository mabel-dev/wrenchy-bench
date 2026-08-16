output "actions_role_arn" {
  description = "Set this as the AWS_BENCH_ROLE_ARN repository secret"
  value       = aws_iam_role.actions.arn
}

output "gcp_key_secret" {
  description = "Put the read-only GCP service-account key here before the first run"
  value       = aws_secretsmanager_secret.gcp_reader.name
}

output "results_bucket" {
  value = "s3://${aws_s3_bucket.results.id}"
}

output "alerts_topic_arn" {
  description = "Subscribe an address to this to hear about runaway instances"
  value       = aws_sns_topic.alerts.arn
}
