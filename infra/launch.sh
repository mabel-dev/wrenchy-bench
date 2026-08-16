#!/usr/bin/env bash
#
# Launch one benchmark instance. Called by .github/workflows/weekly.yml with
# AWS credentials already assumed via OIDC.
#
# Reads: RUN_ID ENGINE_REF INSTANCE_TYPE CORPUS_PREFIX RESULTS_BUCKET AWS_REGION
# Writes: instance_id to $GITHUB_OUTPUT

set -Eeuo pipefail

: "${RUN_ID:?}" "${CORPUS_PREFIX:?}" "${RESULTS_BUCKET:?}"
ENGINE_REF="${ENGINE_REF:-main}"
INSTANCE_TYPE="${INSTANCE_TYPE:-c8g.4xlarge}"
HARNESS_REF="${GITHUB_SHA:-main}"

# Terraform owns these; read them rather than hardcoding, so a re-apply that
# moves the subnet does not silently launch into the wrong VPC.
read -r SUBNET_ID SG_ID PROFILE_ARN <<<"$(
    aws ssm get-parameters \
        --names /opteryx-bench/subnet-id /opteryx-bench/security-group-id /opteryx-bench/instance-profile-arn \
        --query 'Parameters[].Value' --output text
)"

# Ubuntu 24.04 arm64, resolved at launch rather than pinned: a stale AMI is a
# stale kernel, and the kernel is recorded in the run manifest anyway, so a
# change is visible rather than hidden.
AMI_ID=$(aws ssm get-parameter \
    --name /aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id \
    --query 'Parameter.Value' --output text)

USER_DATA=$(
    {
        echo "#!/usr/bin/env bash"
        echo "export ENGINE_REF='${ENGINE_REF}'"
        echo "export HARNESS_REF='${HARNESS_REF}'"
        echo "export CORPUS_PREFIX='${CORPUS_PREFIX}'"
        echo "export RESULTS_BUCKET='${RESULTS_BUCKET}'"
        echo "export RUN_ID='${RUN_ID}'"
        tail -n +2 bootstrap/user-data.sh
    } | base64 -w0
)

# 200GB: ~70GB of corpora, the build tree, and headroom. Throughput is raised
# from the 125 MB/s gp3 default because TPC-H SF100 is 40GB against 32GiB of
# RAM — that line genuinely reads from disk, so at the default the storage
# would be a large part of what the benchmark measures.
BLOCK_DEVICES='[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":200,"VolumeType":"gp3","Throughput":500,"Iops":6000,"DeleteOnTermination":true}}]'

echo "launching ${INSTANCE_TYPE} for run ${RUN_ID} (engine ${ENGINE_REF})"

INSTANCE_ID=$(aws ec2 run-instances \
    --image-id "${AMI_ID}" \
    --instance-type "${INSTANCE_TYPE}" \
    --subnet-id "${SUBNET_ID}" \
    --security-group-ids "${SG_ID}" \
    --iam-instance-profile "Arn=${PROFILE_ARN}" \
    --block-device-mappings "${BLOCK_DEVICES}" \
    --user-data "${USER_DATA}" \
    --instance-initiated-shutdown-behavior terminate \
    --metadata-options "HttpTokens=required,HttpEndpoint=enabled" \
    --tag-specifications \
    "ResourceType=instance,Tags=[{Key=Name,Value=opteryx-bench-${RUN_ID}},{Key=project,Value=wrenchy-bench},{Key=run_id,Value=${RUN_ID}},{Key=engine_ref,Value=${ENGINE_REF}}]" \
    --query 'Instances[0].InstanceId' --output text)

echo "instance_id=${INSTANCE_ID}" >>"${GITHUB_OUTPUT:-/dev/stdout}"
echo "launched ${INSTANCE_ID}"

# ---------------------------------------------------------------------------
# Kill-switch: terminate the instance if it is still alive 8 hours from now.
#
# There are already two ways it should die — `shutdown -h +420` set as the first
# command in user-data, and the collect job's always() terminate step. Both have
# the same blind spot: they assume something is still working. If user-data
# never got as far as arming the watchdog, or the kernel wedges, or the whole
# workflow run is cancelled, nothing terminates the box and it bills until
# someone notices.
#
# This needs no scheduler and no Lambda. A CloudWatch alarm can carry the EC2
# terminate action directly (`arn:aws:automate:<region>:ec2:terminate`), and
# "8 consecutive hourly datapoints exist for this instance" is exactly the
# condition "it has been up for 8 hours". Costs $0.10/alarm-month, and the
# collect job deletes it on the way out.
# ---------------------------------------------------------------------------
aws cloudwatch put-metric-alarm \
    --alarm-name "opteryx-bench-killswitch-${INSTANCE_ID}" \
    --alarm-description "Terminate ${INSTANCE_ID}: still running 8h after launch" \
    --namespace AWS/EC2 --metric-name CPUUtilization \
    --dimensions "Name=InstanceId,Value=${INSTANCE_ID}" \
    --statistic Maximum --period 3600 --evaluation-periods 8 \
    --threshold -1 --comparison-operator GreaterThanThreshold \
    --treat-missing-data notBreaching \
    --alarm-actions "arn:aws:automate:${AWS_REGION:-us-east-1}:ec2:terminate"

echo "kill-switch armed: opteryx-bench-killswitch-${INSTANCE_ID}"

# On-demand, not spot. An interruption at hour four wastes the whole run, and
# a $3 run does not justify checkpointing or retry logic to survive one.
