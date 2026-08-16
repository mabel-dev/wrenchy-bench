output "actions_role_arn" {
  description = "Set this as the AWS_BENCH_ROLE_ARN repository secret"
  value       = aws_iam_role.actions.arn
}

output "corpus_prefix" {
  description = "Set as CORPUS_PREFIX in the workflow, with the version suffix appended"
  value       = "s3://${aws_s3_bucket.corpora.id}"
}

output "results_bucket" {
  value = "s3://${aws_s3_bucket.results.id}"
}

output "alerts_topic_arn" {
  description = "Subscribe an address to this to hear about runaway instances"
  value       = aws_sns_topic.alerts.arn
}

output "launcher_function" {
  description = "Invoke this to start a run by hand: aws lambda invoke --function-name <this> /dev/stdout"
  value       = aws_lambda_function.launcher.function_name
}
