"""The suite definition: seven lines, their corpora, and how each is invoked.

This module is the single place the suite is described. The bootstrap script,
the corpus publisher, the normaliser and the site builder all read it, so a
line cannot be added to the run without also being added to the corpus sync
and the results schema.

Deliberately stdlib-only and import-free of opteryx: it is read on the control
side (corpus publishing, comparison) as well as on the benchmark box.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Corpora
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Corpus:
    """A stored dataset, built once and synced read-only onto the box.

    ``codec`` is carried here and never inferred at run time. The skene mirrors
    are lz4 (WriteOptions::for_fast_reads, the local-benchmark posture) and the
    parquet corpora are zstd/snappy (the storage posture). That asymmetry is a
    standing decision, which means a skene number and a parquet number differ
    by codec as well as by format — so the codec travels with every result row
    and is printed beside every figure.
    """

    name: str
    dest: str  # path under the opteryx-core checkout
    codec: str
    approx_bytes: int


CORPORA = {
    c.name: c
    for c in (
        Corpus("tpch_1_skene", "testdata/tpch_1_skene", "lz4", 300_000_000),
        Corpus("tpch_10_skene", "testdata/tpch_10_skene", "lz4", 4_000_000_000),
        Corpus("tpch_100_skene", "testdata/tpch_100_skene", "lz4", 40_000_000_000),
        Corpus("job", "testdata/job", "snappy", 1_800_000_000),
        Corpus("h2o", "testdata/h2o", "zstd", 5_000_000_000),
        Corpus("hits_rugo_262k", "scratch/hits_rugo_262k", "zstd", 8_100_000_000),
        Corpus("hits_skene", "scratch/hits_skene", "lz4", 14_000_000_000),
    )
}


# --------------------------------------------------------------------------
# Suite lines
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Line:
    """One benchmark line in the weekly run.

    ``argv`` is run from the opteryx-core checkout root with BENCH_PRELOAD in
    the environment. We invoke the runners directly rather than through their
    make targets because the make targets also *generate* corpora on a cache
    miss — on this box a missing corpus must be a hard failure, never a
    forty-minute silent rebuild in the middle of a timed run.
    """

    id: str
    label: str
    benchmark: str  # tpch | job | h2o | clickbench
    scale_factor: str | None
    data_format: str  # skene | parquet
    corpus: str  # key into CORPORA
    argv: list[str]
    reader: str  # which adapter parses the output: tpch_csv | job_csv | h2o_csv | clickbench_json
    results_dir: str | None = None  # runner-owned dir it drops a CSV into
    json_out: bool = False  # runner writes JSON to a path we pass
    extra_corpora: list[str] = field(default_factory=list)

    @property
    def codec(self) -> str:
        return CORPORA[self.corpus].codec


_TPCH = "tests/performance/tpch/runner.py"
_JOB = "tests/performance/job/runner.py"
_H2O = "tests/performance/h2o/runner.py"
_CLICKBENCH = "tests/performance/clickbench/opteryx/runner.py"

SUITE: list[Line] = [
    Line(
        id="tpch_sf1_skene",
        label="TPC-H SF1 · Skene",
        benchmark="tpch",
        scale_factor="1",
        data_format="skene",
        corpus="tpch_1_skene",
        argv=[_TPCH, "--scale", "1", "--variant", "skene"],
        reader="tpch_csv",
        results_dir="tests/performance/tpch/results",
    ),
    Line(
        id="tpch_sf10_skene",
        label="TPC-H SF10 · Skene",
        benchmark="tpch",
        scale_factor="10",
        data_format="skene",
        corpus="tpch_10_skene",
        argv=[_TPCH, "--scale", "10", "--variant", "skene"],
        reader="tpch_csv",
        results_dir="tests/performance/tpch/results",
    ),
    Line(
        id="tpch_sf100_skene",
        label="TPC-H SF100 · Skene",
        benchmark="tpch",
        scale_factor="100",
        data_format="skene",
        corpus="tpch_100_skene",
        argv=[_TPCH, "--scale", "100", "--variant", "skene"],
        reader="tpch_csv",
        results_dir="tests/performance/tpch/results",
    ),
    Line(
        # JOB and H2O do not stipulate a storage format — upstream ships rows
        # (CSV from CWI / datagen.R), not files. Parquet here is the harness's
        # choice, so it is named explicitly rather than left implicit.
        id="job_parquet",
        label="JOB · Parquet",
        benchmark="job",
        scale_factor=None,
        data_format="parquet",
        corpus="job",
        # 60s, not the 300s default: 113 queries x 2 iterations x 300s is 18.8h
        # worst case against a 4-5h budget. A query crossing 60s is a regression
        # whether or not it would have finished at 300s, so the bound loses
        # nothing the trend needs — but timeout_count is a headline metric so it
        # can never hide one either.
        argv=[_JOB, "--timeout", "60", "--iterations", "2"],
        reader="job_csv",
        results_dir="tests/performance/job/results",
    ),
    Line(
        id="h2o_parquet",
        label="H2O · Parquet (medium)",
        benchmark="h2o",
        scale_factor="medium",
        data_format="parquet",
        corpus="h2o",
        # medium (1e8 rows, ~5GB) over the small default: 630MB sits entirely in
        # page cache on a 32GiB box, which measures compute with the storage
        # layer removed.
        argv=[_H2O, "--workload", "both", "--size", "medium", "--timeout", "300"],
        reader="h2o_csv",
        results_dir="tests/performance/h2o/results",
    ),
    Line(
        id="clickbench_parquet",
        label="ClickBench · Parquet partitioned",
        benchmark="clickbench",
        scale_factor=None,
        data_format="parquet",
        corpus="hits_rugo_262k",
        # --variant parquet, NOT the bare default: as of 2026-08-16 the runner's
        # VARIANT_DATASETS[""] resolves to the skene mirror, so an unqualified
        # run benchmarks skene under the parquet line's name. See PREFLIGHT.md.
        argv=[_CLICKBENCH, "--variant", "parquet", "--iterations", "5"],
        reader="clickbench_json",
        json_out=True,
    ),
    Line(
        id="clickbench_skene",
        label="ClickBench · Skene",
        benchmark="clickbench",
        scale_factor=None,
        data_format="skene",
        corpus="hits_skene",
        argv=[_CLICKBENCH, "--variant", "skene", "--iterations", "5"],
        reader="clickbench_json",
        json_out=True,
    ),
]

SUITE_BY_ID = {line.id: line for line in SUITE}

# The calibration bookend. Run first and last; a divergence beyond
# CALIBRATION_TOLERANCE means the machine moved under the run and every number
# in it is suspect. One minute at each end, on a line already in the suite.
CALIBRATION_LINE = "tpch_sf1_skene"
CALIBRATION_TOLERANCE = 0.10

# Regression thresholds, applied to the minimum across iterations.
REGRESSION_RATIO = 1.15  # vs trailing baseline, needs two consecutive runs
IMMEDIATE_RATIO = 1.25  # reported on a single run
BASELINE_RUNS = 4  # trailing non-suspect runs forming the baseline

# A query whose per-round spread exceeds this fraction of its own minimum is
# UNSTABLE: the machine moved under it, so its minimum is not a usable signal.
# Matches the ClickBench runner's own threshold.
UNSTABLE_SPREAD = 0.45

TELEMETRY_WORKSPACE = "opteryx"
TELEMETRY_COLLECTION = "telemetry"
TABLE_QUERIES = "benchmarks"
TABLE_RUNS = "benchmark_runs"
TABLE_OPERATIONS = "benchmark_operations"
