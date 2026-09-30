# Development

How this harness is built, run, and operated.

## How a run happens

The box installs the **published wheel** rather than building from source. A
GitHub Actions workflow launches one run daily at 11:00 UTC by invoking the AWS
launcher Lambda. It does not check PyPI or compare versions; the Lambda uses its
configured engine version (latest by default). The returned run ID is passed to
the collector workflow, which waits for the bundle, compares results against
history, publishes telemetry, and updates the site. A run typically takes about
20 minutes; the 1-hour kill-switch bounds a runaway instance.

The version is recorded per run, so a number is attributable to an exact
release rather than to "whatever main was".

Start a run by hand with:

```bash
aws lambda invoke --function-name opteryx-bench-launcher \
  --payload '{}' --cli-binary-format raw-in-base64-out /tmp/launch.json
cat /tmp/launch.json
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
per run is ~$9, or about $270/month at daily cadence, more than the compute it
feeds. In-region S3 transfer avoids that egress cost.

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

## TPC-H validation and conformance

`queries/tpch/*.sql` are templates over `@NAME@` parameters
(`harness/tpch_params.py`): `BENCH` is what the suite runs, `VALIDATION` is the
spec's Clause 2.4.x.4 set. To check the engine's answers at SF1 against the TPC
answer set (needs `pip install duckdb`):

```bash
cd <dir containing testdata/>
python harness/validate_tpch.py --relation testdata.tpch_1_skene
```

Known results and why (2026-09-28, local opteryx-core 0.9.143+3596: 21 PASS, 1 DATA):

- **q06** used to FAIL on any wheel without opteryx-core commit `ebf96999`: the
  constant folder computed `0.06 + 0.01` in floating point (0.06999999999999999),
  so `l_discount between 0.06 - 0.01 and 0.06 + 0.01` dropped every 0.07 row
  (75,207,768 vs 123,141,078). Fixed by folding decimal-point literal arithmetic
  exactly. Daily runs on older wheels timed a wrong-result Q6, so its history
  before that release is not comparable.
- **q13 DATA**: the corpora's free-text columns (every comment and address) do
  not match DuckDB's dbgen; keys, numbers, dates and enums are identical.
  Queries that only print a text column are compared with it masked.
- **Deviations from the spec text** (both engine limits, see the query headers):
  q13 (non-equality predicate in ON) and q15 (Appendix B Variant A does not run).
  Q17's `CAST` and the `LIMIT`/`::DATE` syntax are permitted minor modifications.

## Setup

```bash
cd infra && terraform init && terraform apply \
    -var vpc_id=vpc-… -var subnet_id=subnet-…
```

Then set `AWS_BENCH_ROLE_ARN` (from the terraform output), `OPTERYX_CLIENT_ID`
and `OPTERYX_CLIENT_SECRET` as repository secrets, build and publish the corpora,
and read [PREFLIGHT.md](PREFLIGHT.md).

### Runaway protection

Two layers, each covering the other's blind spot: the instance self-terminates
after its run completes (or times out), which assumes it got there and the
kernel is responsive; and a 1-hour CloudWatch alarm carrying the native
`arn:aws:automate:<region>:ec2:terminate` action, armed by the launcher,
which assumes nothing. A run that wedges is terminated by the alarm. The
collector holds no `ec2` write permissions at all - terminating is AWS's job.
