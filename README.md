# Wrenchy Bench

Weekly performance baselining for [Opteryx](https://github.com/mabel-dev/opteryx-core).

Seven workloads, one `c8g.4xlarge`, once a week. Corpora are built once and
stored, never regenerated. Results land in `opteryx.benchmarks.telemetry` and
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

The box installs the **published wheel** rather than building from source, so
a number is attributable to an exact opteryx-core release rather than to
"whatever main was". The version is recorded on every run.

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

## Development

Building, running, and operating this harness — the AWS/GitHub pipeline,
corpus builds, local runs, and infrastructure setup — is covered in
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).
