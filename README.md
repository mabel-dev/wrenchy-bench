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
| ClickBench partitioned | Parquet · zstd | `hits_rugo_262k` | 43 | 5 | 8.1 GB |
| ClickBench | Skene · lz4 | `hits_skene` | 43 | 5 | 14 GB |

271 queries, ≈ 4 hours, ≈ $3 a run. Stock CPython 3.14 — execution is native
and already runs with the GIL released, so the free-threaded build bought
nothing.

> **JOB and H2O do not stipulate a storage format.** Neither upstream benchmark
> ships one — both distribute *rows* (CSV from CWI, and `datagen.R`), not files.
> The format was always this harness's choice, so it is made once and made the
> same everywhere: Skene, like every other line. ClickBench-parquet stays
> parquet because it *is* the parquet line.

## How a run happens

GitHub Actions is the control plane. The instance's only job is to produce a
run bundle and put it in S3 — it holds no GitHub and no Opteryx credentials.

```
.github/workflows/weekly.yml   Sunday 02:00 UTC
  └── infra/launch.sh          run-instances with bootstrap/user-data.sh as user-data
        └── the box            shutdown -h +420 (watchdog, first)
                               make compile at the pinned engine ref
                               aws s3 sync the corpora, verify every manifest
                               calibration bookend, seven lines, closing bookend
                               write the bundle to S3, STATUS last
                               shutdown -h now
  └── infra/wait-for-run.sh    poll for STATUS
  └── harness/report.py        compare against history, write site/data
  └── harness/publish.py       commit rows to opteryx.telemetry.*
  └── commit + Pages deploy
```

Splitting `launch` from `collect` means the 6-hour GitHub job ceiling never
decides whether a run counts: if `collect` is cancelled the instance still
finishes and still writes its bundle, and re-running `collect` alone picks it
up.

## Layout

```
queries/            271 vendored .sql files — a benchmark repo owns its queries
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
infra/            terraform + the launcher and its kill-switch
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

Three layers, because each has a blind spot the next covers: `shutdown -h +420`
armed as the first command in user-data; the collect job's `always()` terminate
step; and an 8-hour CloudWatch alarm carrying the native
`arn:aws:automate:<region>:ec2:terminate` action, armed per-instance at launch.
The third needs no scheduler and no Lambda, and is the one that still works when
the workflow run itself is cancelled.

## History

This repository previously held a 2024-era correctness harness comparing
Opteryx to MySQL/Postgres/DuckDB. It targeted the pre-1.0 layout (`orso`, a
sibling `../../opteryx` checkout) and no longer ran. It is preserved at tag
`v0-legacy` rather than deleted. Its correctness-comparison idea is worth
revisiting: this suite records `row_count` and `column_count` on every query,
which catches a changed answer, but nothing here checks the answer is *right*.
