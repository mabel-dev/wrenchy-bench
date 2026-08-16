"""Launch the weekly benchmark instance. Fired by EventBridge Scheduler.

Replaces the GitHub Actions launch job. The point is not fewer parts — it is
roughly parts-neutral — but that nothing outside this AWS account can start an
EC2 instance in it. There is no OIDC provider and no external principal with
`ec2:RunInstances`.

Stdlib + boto3 only (boto3 is in the Lambda runtime), so there is nothing to
package: the function is a single file uploaded as a zip by terraform.

Reads its configuration from environment variables set by terraform, and the
bootstrap script from S3 by version — the script is data, not code baked into
the function, so changing what the box runs does not mean redeploying a Lambda.
"""

from __future__ import annotations

import base64
import datetime
import json
import os

import boto3

ec2 = boto3.client("ec2")
s3 = boto3.client("s3")
ssm = boto3.client("ssm")
sns = boto3.client("sns")
cloudwatch = boto3.client("cloudwatch")

REGION = os.environ["AWS_REGION"]
BOOTSTRAP_BUCKET = os.environ["BOOTSTRAP_BUCKET"]
BOOTSTRAP_KEY = os.environ["BOOTSTRAP_KEY"]
CORPUS_PREFIX = os.environ["CORPUS_PREFIX"]
RESULTS_BUCKET = os.environ["RESULTS_BUCKET"]
ALERTS_TOPIC = os.environ["ALERTS_TOPIC"]
INSTANCE_TYPE = os.environ.get("INSTANCE_TYPE", "c8g.4xlarge")
ENGINE_VERSION = os.environ.get("ENGINE_VERSION", "latest")
HARNESS_REF = os.environ.get("HARNESS_REF", "main")

# Ubuntu 24.04 arm64, resolved at launch rather than pinned: a stale AMI is a
# stale kernel, and the kernel is recorded in the run manifest anyway, so a
# change is visible rather than hidden.
AMI_PARAM = "/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id"

# 200GB: ~96GB of corpora, the build tree, and headroom. Throughput is raised
# from the 125 MB/s gp3 default because TPC-H SF100 is 42.6GB against 32GiB of
# RAM — that line genuinely reads from disk, so at the default the storage
# would be a large part of what the benchmark measures.
BLOCK_DEVICES = [
    {
        "DeviceName": "/dev/sda1",
        "Ebs": {
            "VolumeSize": 200,
            "VolumeType": "gp3",
            "Throughput": 500,
            "Iops": 6000,
            "DeleteOnTermination": True,
        },
    }
]


def _param(name: str) -> str:
    return ssm.get_parameter(Name=name)["Parameter"]["Value"]


def _user_data(run_id: str) -> str:
    body = s3.get_object(Bucket=BOOTSTRAP_BUCKET, Key=BOOTSTRAP_KEY)["Body"].read().decode()
    # The script's own shebang is dropped; the exports go between it and the body.
    body = body.split("\n", 1)[1] if body.startswith("#!") else body
    header = "\n".join(
        [
            "#!/usr/bin/env bash",
            f"export ENGINE_VERSION='{ENGINE_VERSION}'",
            f"export HARNESS_REF='{HARNESS_REF}'",
            f"export CORPUS_PREFIX='{CORPUS_PREFIX}'",
            f"export RESULTS_BUCKET='{RESULTS_BUCKET}'",
            f"export RUN_ID='{run_id}'",
            "",  # the body's first line is a bare `#`; without this separator
        ]      # it lands on the end of the last export and bash reads
    )          # `'...Z'#` as ONE word, silently appending # to the value
    return base64.b64encode((header + "\n" + body.lstrip("\n")).encode()).decode()


def _arm_killswitch(instance_id: str) -> None:
    """Terminate the instance if it is still alive 8 hours from now.

    The instance carries its own `shutdown -h +420`, but that assumes user-data
    got far enough to arm it and that the kernel is still responsive. This does
    not: a CloudWatch alarm carries the native EC2 terminate action, and eight
    consecutive hourly datapoints with a -1 threshold simply means "alive for
    eight hours" — any published datapoint clears -1, busy or idle.
    """
    cloudwatch.put_metric_alarm(
        AlarmName=f"opteryx-bench-killswitch-{instance_id}",
        AlarmDescription=f"Terminate {instance_id}: still running 8h after launch",
        Namespace="AWS/EC2",
        MetricName="CPUUtilization",
        Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
        Statistic="Maximum",
        Period=3600,
        EvaluationPeriods=8,
        Threshold=-1,
        ComparisonOperator="GreaterThanThreshold",
        TreatMissingData="notBreaching",
        AlarmActions=[
            f"arn:aws:automate:{REGION}:ec2:terminate",
            ALERTS_TOPIC,
        ],
    )


def handler(event, context):
    run_id = (event or {}).get("run_id") or datetime.datetime.now(
        datetime.timezone.utc
    ).strftime("%Y-%m-%dT%H:%MZ")

    try:
        response = ec2.run_instances(
            ImageId=_param(AMI_PARAM),
            InstanceType=(event or {}).get("instance_type", INSTANCE_TYPE),
            MinCount=1,
            MaxCount=1,
            SubnetId=_param("/opteryx-bench/subnet-id"),
            SecurityGroupIds=[_param("/opteryx-bench/security-group-id")],
            IamInstanceProfile={"Arn": _param("/opteryx-bench/instance-profile-arn")},
            BlockDeviceMappings=BLOCK_DEVICES,
            UserData=_user_data(run_id),
            # On-demand, not spot: an interruption at hour four wastes the run,
            # and $3 does not justify checkpointing to survive one.
            InstanceInitiatedShutdownBehavior="terminate",
            MetadataOptions={"HttpTokens": "required", "HttpEndpoint": "enabled"},
            TagSpecifications=[
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": "Name", "Value": f"opteryx-bench-{run_id}"},
                        {"Key": "project", "Value": "wrenchy-bench"},
                        {"Key": "run_id", "Value": run_id},
                        {"Key": "engine_version", "Value": ENGINE_VERSION},
                    ],
                }
            ],
        )
    except Exception as exception:
        # A launch that fails silently is a week with no data and no signal.
        sns.publish(
            TopicArn=ALERTS_TOPIC,
            Subject=f"wrenchy-bench: launch FAILED for {run_id}",
            Message=f"run_instances raised {type(exception).__name__}: {exception}",
        )
        raise

    instance_id = response["Instances"][0]["InstanceId"]
    _arm_killswitch(instance_id)

    print(json.dumps({"run_id": run_id, "instance_id": instance_id}))
    return {"run_id": run_id, "instance_id": instance_id}
