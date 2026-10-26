# Wrenchy Bench

Daily performance baselining for [Opteryx](https://github.com/mabel-dev/opteryx-core).

Eight workloads, one `c8g.4xlarge`, daily. Corpora are built once
and stored, never regenerated. Results land in `opteryx.benchmarks.telemetry`
and on a [GitHub Pages site](https://mabel-dev.github.io/wrenchy-bench/).

## Purpose

We run and publish these benchmarks to track the engine's performance over time
across a number of different benchmarks, in order to:

1. **identify regressions** as they are introduced;
2. **confirm improvements** to the engine; and
3. **demonstrate that we are not over-fitting** to any single benchmark.

This is an engineering tracker for Opteryx's own development. We provide no
mechanism as part of this tracking to compare or contrast Opteryx with other
systems.

| Line | Format | Corpus | Queries | Iterations | Size |
|---|---|---|--:|--:|--:|
| TPC-H SF1 | Parquet (rugo) · zstd | `tpch_1_rugo` | 22 | 5 | 0.3 GB |
| TPC-H SF10 | Parquet (rugo) · zstd | `tpch_10_rugo` | 22 | 5 | 3.1 GB |
| TPC-H SF100 | Parquet (rugo) · zstd | `tpch_100_rugo` | 22 | 3 | 32.4 GB |
| JOB | Parquet (rugo) · zstd | `job_rugo` | 113 | 3 | 1.5 GB |
| H2O (medium) | Parquet (rugo) · zstd | `h2o_rugo` | 15 | 3 | 7.9 GB |
| ClickBench partitioned | Parquet · zstd | `hits_partitioned` | 43 | 5 | 14.8 GB |
| ClickBench rewritten | Parquet (rugo) · zstd | `hits_rugo` | 43 | 5 | 8.8 GB |
| ClickBench | Skene · lz4 | `hits_skene` | 43 | 5 | 12.4 GB |

323 queries, ≈ 22 minutes per run. Stock CPython 3.14.

> **ClickBench rewritten** is the canonical files rewritten through rugo's own
> writer at its defaults (zstd, 64k-row row groups, column-major blocks of 4) —
> the data the ClickBench entry "Opteryx (Parquet, rewritten)" runs on. It sits
> beside the canonical line, not in place of it.

> **ClickBench parquet is the canonical upstream corpus** — the 100 objects
> from `datasets.clickhouse.com/hits_compatible/athena_partitioned/` that every
> published ClickBench "Parquet (partitioned)" figure is measured against.
> This line exists to sit beside DuckDB and ClickHouse on identical bytes;
> the Skene line is where our format is tested.

> **TPC-H, JOB and H2O do not stipulate a storage format.** None of the three
> upstream benchmarks ships one — they distribute *rows* (`dbgen`, CSV from CWI,
> and `datagen.R`), not files. The format is this harness's choice, so it is made
> once and made the same everywhere: rugo parquet, and it is not part of a
> line's name. They were Skene mirrors until 2026-10-21; the lines were renamed
> (`tpch_sf1_skene` → `tpch_sf1`, …) so each starts a clean baseline rather than
> being compared against a different format. ClickBench keeps its three
> formats, because there the format is the point.

The box installs the **published wheel** rather than building from source, so
a number is attributable to an exact opteryx-core release rather than to
"whatever main was". The version is recorded on every run.

## Comparability

Each benchmark's job is to make this run comparable to the previous run.

Results are compared against the trailing baseline (four non-suspect runs).

- **Codec postures differ by design.** The ClickBench Skene mirror is `lz4` (the
  local benchmark posture); parquet corpora are `zstd`/`snappy` (the storage
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

## Licensing and attribution

This repository's own code (the harness, infrastructure, site and scripts) is
licensed under the Apache License 2.0, see [LICENSE](LICENSE). **That licence
does not cover the third-party benchmark material**: the queries under
`queries/` and the corpora derived from the benchmarks' data keep the terms of
their upstream sources, as set out below and in a `NOTICE.md` beside each set
of queries. All of it is used only for the purpose set out under
[Purpose](#purpose), and the corpora are held in private storage, not
published for download.

| Benchmark | Source | Terms | Notice |
|---|---|---|---|
| ClickBench | [ClickHouse, Inc.](https://github.com/ClickHouse/ClickBench) | [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/) | [queries/clickbench/NOTICE.md](queries/clickbench/NOTICE.md) |
| TPC-H | [Transaction Processing Performance Council](https://www.tpc.org/) | TPC fair use policy | [queries/tpch/NOTICE.md](queries/tpch/NOTICE.md) |
| JOB | Leis et al., VLDB 2015; data from [IMDb](https://www.imdb.com) | IMDb non-commercial terms | [queries/job/NOTICE.md](queries/job/NOTICE.md) |
| H2O | [H2O.ai db-benchmark](https://github.com/h2oai/db-benchmark) | [MPL-2.0](https://www.mozilla.org/MPL/2.0/) | [queries/h2o/NOTICE.md](queries/h2o/NOTICE.md) |

- **ClickBench.** The queries have `hits` replaced by a `{DATASET}`
  placeholder and some expressions adapted to Opteryx's SQL dialect; the
  corpora (`hits_rugo`, `hits_skene`) are the upstream `hits` data rewritten
  into other storage formats. These materials stay under CC BY-NC-SA 4.0.
- **TPC-H.** These are not TPC-H results. They are derived from TPC-H, are not
  audited or reviewed by the TPC, and are not comparable to published TPC-H
  results. "TPC-H" identifies the workload the queries derive from; it does
  not claim a TPC result.
- **JOB.** The data is derived from IMDb. Information courtesy of IMDb
  (https://www.imdb.com). Used with permission.
- **H2O.** The `.sql` files derived from db-benchmark remain under MPL-2.0.

## Development

Building, running, and operating this harness — the AWS/GitHub pipeline,
corpus builds, local runs, and infrastructure setup — is covered in
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).
