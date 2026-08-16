# Wrenchy Bench

Weekly performance baselining for [Opteryx](https://github.com/mabel-dev/opteryx-core).

Seven workloads, one `c8g.4xlarge`, once a week. Corpora are built once and
stored, never regenerated. Results land in `opteryx.telemetry.benchmarks` and
on a [GitHub Pages site](https://mabel-dev.github.io/wrenchy-bench/).

| Line | Format | Corpus | Size |
|---|---|---|---|
| TPC-H SF1 | Skene · lz4 | `tpch_1_skene` | 0.3 GB |
| TPC-H SF10 | Skene · lz4 | `tpch_10_skene` | 4.0 GB |
| TPC-H SF100 | Skene · lz4 | `tpch_100_skene` | 40 GB |
| JOB | Parquet · snappy | `job` | 1.8 GB |
| H2O (medium) | Parquet · zstd | `h2o` | 5.0 GB |
| ClickBench partitioned | Parquet · zstd | `hits_rugo_262k` | 8.1 GB |
| ClickBench | Skene · lz4 | `hits_skene` | 14 GB |

≈ 4–5 hours, ≈ $3 a run.

> **JOB and H2O do not stipulate a storage format.** Neither upstream benchmark
> ships one — both distribute *rows* (CSV from CWI, and `datagen.R`), not files.
> Parquet is this harness's choice, so it is named explicitly rather than left
> implicit. Skene mirrors for both are a phase-two addition.

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
harness/
  config.py     the seven lines, their corpora, and the thresholds
  run_suite.py  drives the lines on the box, writes the run bundle
  probe.py      peak RSS / CPU / block I/O — stdlib only, vendorable into opteryx-core
  adapters.py   three CSV shapes + one JSON payload -> one record shape
  manifest.py   corpus manifests: build at publish, verify before every run
  schema.py     the opteryx.telemetry.* table contracts
  report.py     regression detection and the site data
  publish.py    Parquet via rugo, committed through the upload service
corpus/publish.py   build a corpus manifest and push it to S3
infra/              terraform: buckets, OIDC role, instance profile, alarm
bootstrap/          what the instance runs
site/               the static results browser
docs/PREFLIGHT.md   the opteryx-core changes this depends on
```

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
and `OPTERYX_CLIENT_SECRET` as repository secrets, publish each corpus with
`corpus/publish.py`, and land the changes in [docs/PREFLIGHT.md](docs/PREFLIGHT.md).

## History

This repository previously held a 2024-era correctness harness comparing
Opteryx to MySQL/Postgres/DuckDB. It targeted the pre-1.0 layout (`orso`, a
sibling `../../opteryx` checkout) and no longer ran. It is preserved at tag
`v0-legacy` rather than deleted; its correctness-comparison idea is worth
revisiting, since ClickBench still checks no result shapes at all
(PREFLIGHT §5).
