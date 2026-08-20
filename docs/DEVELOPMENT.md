# Development

How this harness is built, run, and operated.

## How a run happens

The box installs the **published wheel** rather than building from source.
opteryx-core releases four or five times a week, so a wheel tracks development
finer than a weekly benchmark can resolve, measures what users actually get,
and takes the whole toolchain off a machine that only needs to run queries.
The version is recorded per run, so a number is attributable to an exact
release rather than to "whatever main was".

**Launching happens in AWS. GitHub only collects.** Nothing outside the account
can start an EC2 instance in it — the one external identity that exists is
read-only on the results bucket.

```
AWS                                    GitHub Actions
────────────────────────────────────────────────────────────────
EventBridge Scheduler, Sun 02:00 UTC
  └─ Lambda opteryx-bench-launcher
       └─ RunInstances + arms the 8h kill-switch
            └─ the box
               shutdown -h +420 (watchdog, first)
               pip install opteryx-core (the published wheel)
               s3 sync the corpora, verify every manifest
               SF1 bookend, 7 lines, SF1 bookend
               write bundle to S3, STATUS last
               terminates itself
                                       Sun 05:00 UTC (or dispatch)
                                       find newest run, wait for STATUS
                                       compare against history
                                       commit site/data, deploy Pages
                                       publish to opteryx.benchmarks
```

Collection is a separate schedule so a failed comparison can be re-run without
paying for another four-hour benchmark: the bundle is already in S3, so
re-running the workflow picks it up. It also means the 6-hour Actions job
ceiling never decides whether a run counts.

Start a run by hand with:

```bash
aws lambda invoke --function-name opteryx-bench-launcher /dev/stdout
```

## Layout

```
queries/            193 vendored .sql files — a benchmark repo owns its queries
  tpch/ job/ h2o/ clickbench/
harness/
  config.py       the seven lines, their corpora, iterations and thresholds
  bench_runner.py the query driver: loads, binds, runs, measures. One per line
  run_suite.py    orchestrates the lines, writes the run bundle
  probe.py        peak RSS / CPU / block I/O — stdlib only
  manifest.py     corpus manifests: build at publish, verify before every run
  schema.py       the opteryx.benchmarks.* table contracts
  report.py       regression detection and the site data
  publish.py      Parquet via rugo, committed through the upload service
corpus/
  convert.py      build the Skene mirrors from parquet, once
  publish.py      manifest, push to GCS, mirror to S3
infra/            terraform, the launcher Lambda, and the collector's helpers
bootstrap/        what the instance runs
site/             the static results browser
docs/PREFLIGHT.md what this still needs from opteryx-core
```

### Why its own driver

`opteryx-core`'s four `tests/performance/*/runner.py` are the record of how the
historical numbers were produced and are left alone. They are also four
drivers with four output shapes, three of which record no result column count
and none of which record resource or engine telemetry. Owning the driver means
one protocol across all seven lines and every column in the results table
populated from the first run.

## Corpora

Read from **S3** in the runner's region — `s3://opteryx-bench-corpora/<version>/`
— where the transfer is free. GCS to EC2 is internet egress at ~$0.12/GB: ~76 GB
a week is ~$9 a run, ~$40/month, more than the compute it feeds and more than
everything else here combined.

`--also-gcs` writes a second copy to `gs://opteryx_data/benchmarks/<version>/`.
The runner never reads it. It exists because **Opteryx has a GCS filesystem and
no S3 one** (`opteryx/connectors/io_systems/`), so without it the corpora cannot
be referenced from the engine at all. ~$1.75/month in storage, no egress.

```bash
python corpus/convert.py --checkout ../opteryx-core --all      # build the mirrors
python corpus/publish.py --source ../opteryx-core/testdata/job_skene --name job_skene
```

## Local use

```bash
python harness/run_suite.py --data-root <dir with testdata/ and scratch/> \
    --out /tmp/bundle --only tpch_sf1_skene --skip-calibration
python harness/report.py --bundle /tmp/bundle --site-data site/data
python harness/publish.py --bundle /tmp/bundle --dry-run
```

## Setup

```bash
cd infra && terraform init && terraform apply \
    -var vpc_id=vpc-… -var subnet_id=subnet-…
```

Then set `AWS_BENCH_ROLE_ARN` (from the terraform output), `OPTERYX_CLIENT_ID`
and `OPTERYX_CLIENT_SECRET` as repository secrets, build and publish the corpora,
and read [PREFLIGHT.md](PREFLIGHT.md).

### Runaway protection

Two layers, each covering the other's blind spot: `shutdown -h +420` armed as
the first command in user-data, which assumes user-data got that far and the
kernel is responsive; and an 8-hour CloudWatch alarm carrying the native
`arn:aws:automate:<region>:ec2:terminate` action, armed by the launcher, which
assumes nothing. The collector holds no `ec2` write permissions at all —
terminating is AWS's job.
