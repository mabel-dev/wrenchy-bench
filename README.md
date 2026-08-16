# Wrenchy Bench

Weekly performance baselining for [Opteryx](https://github.com/mabel-dev/opteryx-core).

Seven workloads, one `c8g.4xlarge`, once a week. Corpora are built once and
stored, never regenerated. Results land in `opteryx.telemetry.benchmarks` and
on a [GitHub Pages site](https://mabel-dev.github.io/wrenchy-bench/).

| Line | Format | Corpus | Queries | Iterations | Size |
|---|---|---|--:|--:|--:|
| TPC-H SF1 | Skene · lz4 | `tpch_1_skene` | 22 | 5 | 0.3 GB |
| TPC-H SF10 | Skene · lz4 | `tpch_10_skene` | 22 | 5 | 4.0 GB |
| TPC-H SF100 | Skene · lz4 | `tpch_100_skene` | 22 | 3 | 40 GB |
| JOB | Skene · lz4 | `job_skene` | 113 | 3 | 2.5 GB |
| H2O (medium) | Skene · lz4 | `h2o_skene` | 15 | 3 | 7.0 GB |
| ClickBench partitioned | Parquet · zstd | `hits_partitioned` | 43 | 5 | 14.8 GB |
| ClickBench | Skene · lz4 | `hits_skene` | 43 | 5 | 14 GB |

280 queries, ≈ 4 hours, ≈ $3 a run. Stock CPython 3.14 — execution is native
and already runs with the GIL released, so the free-threaded build bought
nothing.

> **ClickBench parquet is the canonical upstream corpus** — the 100 objects
> from `datasets.clickhouse.com/hits_compatible/athena_partitioned/` that every
> published ClickBench "Parquet (partitioned)" figure is measured against. An
> earlier version used the same rows rewritten through rugo's writer, which
> made the corpus consistent with our own writer policy and the *number*
> incomparable with everyone else's. This line exists to sit beside DuckDB and
> ClickHouse on identical bytes; the Skene line is where our format is tested.

> **JOB and H2O do not stipulate a storage format.** Neither upstream benchmark
> ships one — both distribute *rows* (CSV from CWI, and `datagen.R`), not files.
> The format was always this harness's choice, so it is made once and made the
> same everywhere: Skene, like every other line. ClickBench-parquet stays
> parquet because it *is* the parquet line.

## How a run happens

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
               make compile at the pinned engine ref
               s3 sync the corpora, verify every manifest
               SF1 bookend, 7 lines, SF1 bookend
               write bundle to S3, STATUS last
               terminates itself
                                       Sun 05:00 UTC (or dispatch)
                                       find newest run, wait for STATUS
                                       compare against history
                                       commit site/data, deploy Pages
                                       publish to opteryx.telemetry
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
  schema.py       the opteryx.telemetry.* table contracts
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

## Comparability

A weekly benchmark's only job is to make week *n* comparable to week *n−1*.

- **Codec postures differ by design.** Skene mirrors are `lz4` (the local
  benchmark posture); parquet corpora are `zstd`/`snappy` (the storage
  posture). ClickBench Parquet and ClickBench Skene therefore differ by codec
  as well as by format and are **not** a like-for-like format comparison. The
  codec travels on every result row for exactly this reason.
- **Bookend calibration.** TPC-H SF1 runs first and last. A divergence over 10%
  means the machine moved under the run; it is stamped `suspect`, kept, and
  excluded from trend baselines. On a shared-tenancy instance this is worth
  more than any amount of pinning.
- **Corpus hashes.** Every run records the manifest hash of each corpus it read.
  A trend break where the hash moved is attributed to the data, not the engine.
- **Hard failures on silent degradation.** A missing allocator preload, an
  unverified corpus, or a free-threaded interpreter each stop the run rather
  than quietly producing a number that cannot be compared to anything.
- **Regressions** are a query more than 15% over the median of the trailing
  four non-suspect runs, confirmed on two consecutive runs — or 25% on a single
  run. A status transition `ok → error`/`timeout` is reported immediately and
  needs no statistics.

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
python harness/run_suite.py --checkout ../opteryx-core --out /tmp/bundle \
    --only tpch_sf1_skene --skip-calibration
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
and read [docs/PREFLIGHT.md](docs/PREFLIGHT.md).

### Runaway protection

Two layers, each covering the other's blind spot: `shutdown -h +420` armed as
the first command in user-data, which assumes user-data got that far and the
kernel is responsive; and an 8-hour CloudWatch alarm carrying the native
`arn:aws:automate:<region>:ec2:terminate` action, armed by the launcher, which
assumes nothing. The collector holds no `ec2` write permissions at all —
terminating is AWS's job.

## History

This repository previously held a 2024-era correctness harness comparing
Opteryx to MySQL/Postgres/DuckDB. It targeted the pre-1.0 layout (`orso`, a
sibling `../../opteryx` checkout) and no longer ran. It is preserved at tag
`v0-legacy` rather than deleted. Its correctness-comparison idea is worth
revisiting: this suite records `row_count` and `column_count` on every query,
which catches a changed answer, but nothing here checks the answer is *right*.
