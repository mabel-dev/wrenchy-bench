#!/usr/bin/env bash
#
# Work out which run this collection is for.
#
# The launcher lives in AWS now, so the collector is not told a run id — it
# discovers one. Either the caller named it (workflow_dispatch), or we take the
# newest prefix under runs/, which is the one the scheduler started a few hours
# ago. Prefixes are ISO timestamps, so lexical order is chronological order.
#
# Reads: RUN_ID (optional) RESULTS_BUCKET
# Writes: run_id, instance_id to $GITHUB_OUTPUT

set -Eeuo pipefail

: "${RESULTS_BUCKET:?}"

if [[ -n "${RUN_ID:-}" ]]; then
    run_id="${RUN_ID}"
    echo "collecting the run named by the caller: ${run_id}"
else
    run_id=$(aws s3 ls "${RESULTS_BUCKET}/runs/" |
        awk '/PRE/ {print $2}' | sed 's:/$::' | sort | tail -1)
    if [[ -z "${run_id}" ]]; then
        echo "::error::no runs found under ${RESULTS_BUCKET}/runs/ — did the launcher fire?"
        exit 1
    fi
    echo "collecting the newest run: ${run_id}"
fi

# The instance id is only needed to delete the kill-switch alarm afterwards, and
# a run whose instance has already gone is the normal case rather than an error
# — so a miss here is empty, not fatal.
instance_id=$(aws ec2 describe-instances \
    --filters "Name=tag:run_id,Values=${run_id}" \
    --query 'Reservations[0].Instances[0].InstanceId' --output text 2>/dev/null || true)
[[ "${instance_id}" == "None" ]] && instance_id=""

{
    echo "run_id=${run_id}"
    echo "instance_id=${instance_id}"
} >>"${GITHUB_OUTPUT:-/dev/stdout}"
