"""The suite definition: seven lines, their corpora, and how each is run.

The single place the suite is described. The corpus publisher, the query
driver, the run orchestrator and the reporter all read it, so a line cannot be
added to the run without also being added to the corpus sync and the results
schema.

Stdlib-only and import-free of opteryx: read on the control side (publishing,
comparison, CI) as well as on the benchmark box.
"""

from __future__ import annotations

from dataclasses import dataclass

# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

# S3, in the runner's own region, is where the corpora are read from.
#
# GCS to EC2 is internet egress at ~$0.12/GB: ~76GB a week is ~$9 a run, or
# ~$40/month — more than the compute it feeds and more than everything else in
# this system combined. S3 to EC2 in-region is free and 76GB of S3 Standard is
# ~$1.75/month.
S3_CORPUS_PREFIX = "s3://opteryx-bench-corpora"
S3_RESULTS_PREFIX = "s3://opteryx-bench-results"

# The optional engine-facing copy, written by `corpus/publish.py --also-gcs`.
#
# ⛔ It is not redundancy and not a fallback — the runner never reads it. It
# exists only so the corpora are queryable as datasets, because Opteryx has a
# GCS filesystem (opteryx/connectors/io_systems/gcs_filesystem.py) and NO S3
# one. Without this copy the benchmark corpora cannot be referenced from the
# engine at all. It costs ~$1.75/month in storage and no egress, since nothing
# reads it on a schedule.
GCS_BUCKET = "opteryx_data"
GCS_PREFIX = f"gs://{GCS_BUCKET}/benchmarks"

CORPUS_VERSION = "v2026-08"

# Stock CPython, NOT the free-threaded build. Execution is native and already
# runs with the GIL released, so 3.14t bought nothing; opteryx-core also stopped
# publishing cp314t wheels after 0.9.16.
PYTHON_VERSION = "3.14"


# --------------------------------------------------------------------------
# Corpora
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Corpus:
    """A stored dataset, built once and synced read-only onto the box.

    ``codec`` is carried here and never inferred at run time. Skene mirrors are
    lz4 (WriteOptions::for_fast_reads, the local-benchmark posture); the
    remaining parquet corpus is zstd (the storage posture). A skene number and
    a parquet number therefore differ by codec as well as by format, so the
    codec travels with every result row.
    """

    name: str
    dest: str  # path under the opteryx-core checkout
    codec: str
    approx_bytes: int
    # Expected number of top-level tables. A size check alone does not catch a
    # partial corpus: `testdata/tpch_1` shipped with lineitem and none of the
    # other seven tables, and its mirror came out at 246MB — comfortably inside
    # any tolerance around a declared ~300MB, while 20 of the 22 TPC-H queries
    # would have failed on missing tables. The table count catches it exactly.
    tables: int


CORPORA = {
    c.name: c
    for c in (
        Corpus("tpch_1_skene", "testdata/tpch_1_skene", "lz4", 405_000_000, tables=8),
        Corpus("tpch_10_skene", "testdata/tpch_10_skene", "lz4", 4_000_000_000, tables=8),
        Corpus("tpch_100_skene", "testdata/tpch_100_skene", "lz4", 40_000_000_000, tables=8),
        Corpus("job_skene", "testdata/job_skene", "lz4", 2_100_000_000, tables=21),
        # medium (1e8 rows) only. `small` is 630MB, which sits entirely in page
        # cache on a 32GiB box and measures compute with storage removed — it
        # was the local default and is not carried into the suite.
        Corpus("h2o_skene", "testdata/h2o_skene", "lz4", 8_700_000_000, tables=5),
        # ClickBench is a single wide table, so its files sit at the top level
        # rather than under per-table directories.
        #
        # THE CANONICAL UPSTREAM FILES, downloaded from
        # datasets.clickhouse.com/hits_compatible/athena_partitioned/ — the same
        # 100 objects every published ClickBench "Parquet (partitioned)" number
        # is measured against. It replaced `hits_rugo_262k`, which was the same
        # rows rewritten through rugo's own writer at 262,144 rows per row
        # group. That rewrite made the corpus internally consistent with our
        # writer policy and made the number incomparable with everyone else's,
        # which is the opposite of what this line is for: it exists to sit
        # beside DuckDB, ClickHouse and DataFusion on the same data.
        Corpus("hits_partitioned", "scratch/hits_partitioned", "zstd", 14_800_000_000, tables=0),
        Corpus("hits_skene", "scratch/hits_skene", "lz4", 15_300_000_000, tables=0),
    )
}


# --------------------------------------------------------------------------
# Suite lines
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Line:
    """One benchmark line. Driven by harness/bench_runner.py, one process each."""

    id: str
    label: str
    benchmark: str  # tpch | job | h2o | clickbench
    scale_factor: str | None
    data_format: str  # skene | parquet
    corpus: str  # key into CORPORA
    relation: str  # dotted dataset name the queries are bound to
    iterations: int
    timeout_s: float

    @property
    def codec(self) -> str:
        return CORPORA[self.corpus].codec

    @property
    def dest(self) -> str:
        return CORPORA[self.corpus].dest


SUITE: list[Line] = [
    Line(
        id="tpch_sf1_skene",
        label="TPC-H SF1 · Skene",
        benchmark="tpch",
        scale_factor="1",
        data_format="skene",
        corpus="tpch_1_skene",
        relation="testdata.tpch_1_skene",
        iterations=5,
        timeout_s=300,
    ),
    Line(
        id="tpch_sf10_skene",
        label="TPC-H SF10 · Skene",
        benchmark="tpch",
        scale_factor="10",
        data_format="skene",
        corpus="tpch_10_skene",
        relation="testdata.tpch_10_skene",
        iterations=5,
        timeout_s=300,
    ),
    Line(
        id="tpch_sf100_skene",
        label="TPC-H SF100 · Skene",
        benchmark="tpch",
        scale_factor="100",
        data_format="skene",
        corpus="tpch_100_skene",
        relation="testdata.tpch_100_skene",
        iterations=5,
        timeout_s=900,
    ),
    Line(
        id="job_skene",
        label="JOB · Skene",
        benchmark="job",
        scale_factor=None,
        data_format="skene",
        corpus="job_skene",
        relation="testdata.job_skene",
        # JOB runs the whole 113-query suite in ~30s locally, so five
        # iterations is still minutes, not hours. The timeout exists to bound
        # a pathological plan, not to fit the schedule: a JOB query crossing
        # 120s is a regression regardless.
        iterations=5,
        timeout_s=120,
    ),
    Line(
        id="h2o_skene",
        label="H2O · Skene (medium)",
        benchmark="h2o",
        scale_factor="medium",
        data_format="skene",
        corpus="h2o_skene",
        relation="testdata.h2o_skene",
        iterations=5,
        timeout_s=300,
    ),
    Line(
        id="clickbench_parquet",
        label="ClickBench · Parquet partitioned (canonical)",
        benchmark="clickbench",
        scale_factor=None,
        data_format="parquet",
        corpus="hits_partitioned",
        relation="scratch.hits_partitioned",
        iterations=5,
        timeout_s=300,
    ),
    Line(
        id="clickbench_skene",
        label="ClickBench · Skene",
        benchmark="clickbench",
        scale_factor=None,
        data_format="skene",
        corpus="hits_skene",
        relation="scratch.hits_skene",
        iterations=5,
        timeout_s=300,
    ),
]

SUITE_BY_ID = {line.id: line for line in SUITE}

# The calibration bookend. Run first and last; a divergence beyond the
# tolerance means the machine moved under the run, so every number in it is
# suspect. About a minute at each end, on a line already in the suite.
CALIBRATION_LINE = "tpch_sf1_skene"
CALIBRATION_TOLERANCE = 0.10

# Regression thresholds, applied to the minimum across iterations.
REGRESSION_RATIO = 1.15  # vs trailing baseline, needs two consecutive runs
IMMEDIATE_RATIO = 1.25  # reported on a single run
BASELINE_RUNS = 4  # trailing non-suspect runs forming the baseline

# A query whose spread across iterations exceeds this fraction of its own
# minimum is UNSTABLE: the machine moved under it, so its minimum measures the
# machine rather than the engine.
UNSTABLE_SPREAD = 0.45

TELEMETRY_WORKSPACE = "opteryx"
TELEMETRY_COLLECTION = "benchmarks"
TABLE_QUERIES = "telemetry"
TABLE_RUNS = "benchmark_runs"
TABLE_OPERATIONS = "benchmark_operations"
