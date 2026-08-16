variable "region" {
  description = "Region for the buckets and the runner. Must match where the corpora live — cross-region sync of 70GB is neither free nor fast."
  type        = string
  default     = "us-east-1"
}

variable "vpc_id" {
  description = "VPC for the runner. The account has no default VPC, so this is required."
  type        = string
}

variable "subnet_id" {
  description = "Public subnet in vpc_id. run-instances fails with VPCIdNotSpecified without one."
  type        = string
}

variable "github_repo" {
  description = "owner/repo allowed to assume the Actions role"
  type        = string
  default     = "mabel-dev/wrenchy-bench"
}
