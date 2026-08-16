"""The table contracts for opteryx.telemetry.*

Pinned column order and types. These tables are append-only, so a widened
schema cannot be undone without a rewrite — publishing raises rather than
absorbing an unexpected column, and every change here is a reviewed change.

Three tables, because detection, provenance and diagnosis have different row
counts and different lifetimes:

  benchmarks            ~2,000 rows/week   the fact table; what regressions are detected from
  benchmark_runs        1 row/week         the environment a run happened in
  benchmark_operations  on demand          per-operator breakdown, from the tracing pass only
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# opteryx.telemetry.benchmarks — one row per query per iteration
# --------------------------------------------------------------------------

QUERY_COLUMNS: list[tuple[str, str]] = [
    ("run_id", "VARCHAR"),
    # The suite line, e.g. tpch_sf100_skene. Carried explicitly rather than
    # reconstructed from (benchmark, format, scale) downstream: the tuple is
    # not guaranteed unique if a second variant of a line is ever added.
    ("line", "VARCHAR"),
    # A timestamp rather than a date: two runs in one day must still order, and
    # a re-run after a failure is a normal thing to want.
    ("run_date", "VARCHAR"),
    ("engine_version", "VARCHAR"),
    ("engine_build", "INT32"),
    ("benchmark", "VARCHAR"),
    # VARCHAR, not a number — H2O's scales are "small"/"medium" and JOB has none.
    ("scale_factor", "VARCHAR"),
    ("data_format", "VARCHAR"),
    # Carried from the corpus manifest, never inferred. Without it data_format is
    # a trap: the skene mirrors are lz4 and the parquet corpora zstd/snappy, so
    # a format comparison that ignores codec is measuring the codec too.
    ("codec", "VARCHAR"),
    # VARCHAR, not a number — JOB's queries are 1a..33c, H2O's g1..j5.
    ("query", "VARCHAR"),
    ("query_ordinal", "INT32"),
    ("iteration", "INT32"),
    ("cache_state", "VARCHAR"),
    ("duration_ms", "FLOAT64"),
    ("row_count", "INT64"),
    ("column_count", "INT32"),
    ("status", "VARCHAR"),
    ("error", "VARCHAR"),
    # --- process telemetry -------------------------------------------------
    ("peak_rss_bytes", "INT64"),
    ("cpu_ms", "FLOAT64"),
    ("cpu_efficiency", "FLOAT64"),
    ("disk_read_bytes", "INT64"),
    ("major_faults", "INT64"),
    ("involuntary_ctx_switches", "INT64"),
    # --- engine telemetry (Session.telemetry) ------------------------------
    ("bytes_processed", "INT64"),
    ("blobs_seen", "INT64"),
    ("blobs_read", "INT64"),
    ("rows_seen", "INT64"),
    ("rows_read", "INT64"),
    ("columns_read", "INT32"),
    ("time_planning_ms", "FLOAT64"),
]

# --------------------------------------------------------------------------
# opteryx.telemetry.benchmark_runs — one row per run
# --------------------------------------------------------------------------

RUN_COLUMNS: list[tuple[str, str]] = [
    ("run_id", "VARCHAR"),
    ("run_date", "VARCHAR"),
    ("finished", "VARCHAR"),
    ("status", "VARCHAR"),
    ("engine_version", "VARCHAR"),
    ("engine_build", "INT32"),
    # The suite measures a published wheel, so <version>+<build> IS the engine
    # identity and there is no git sha. These stay for runs recorded before the
    # switch, and for a local run against a source checkout.
    ("git_sha", "VARCHAR"),
    ("git_dirty", "BOOLEAN"),
    ("engine_path", "VARCHAR"),
    ("python_version", "VARCHAR"),
    ("gil_enabled", "BOOLEAN"),
    ("allocator_preload", "VARCHAR"),
    ("peak_rss_reset_supported", "BOOLEAN"),
    ("instance_type", "VARCHAR"),
    ("availability_zone", "VARCHAR"),
    ("storage", "VARCHAR"),
    ("kernel", "VARCHAR"),
    ("cpu_model", "VARCHAR"),
    ("cpu_count", "INT32"),
    ("mem_total_bytes", "INT64"),
    # The bookends. A drift beyond the tolerance means the machine moved under
    # the run, and every number in it is excluded from trend baselines.
    ("calibration_open_ms", "FLOAT64"),
    ("calibration_close_ms", "FLOAT64"),
    ("calibration_drift", "FLOAT64"),
    # JSON blob: {corpus_name: listing_sha256}. A trend break where these moved
    # is attributable to the data, not the engine.
    ("corpus_hashes", "VARCHAR"),
    ("suite_wall_ms", "FLOAT64"),
]

# --------------------------------------------------------------------------
# opteryx.telemetry.benchmark_operations — per-operator, tracing pass only
# --------------------------------------------------------------------------

OPERATION_COLUMNS: list[tuple[str, str]] = [
    ("run_id", "VARCHAR"),
    ("benchmark", "VARCHAR"),
    ("scale_factor", "VARCHAR"),
    ("data_format", "VARCHAR"),
    ("query", "VARCHAR"),
    ("node_id", "VARCHAR"),
    ("operator", "VARCHAR"),
    ("self_time_ms", "FLOAT64"),
    ("rows_in", "INT64"),
    ("rows_out", "INT64"),
    # These rows come from a SEPARATE EXPLAIN ANALYZE pass, never from the timed
    # one — tracing perturbs the thing it measures. Marked so they can never be
    # mixed into a timing trend by accident.
    ("pass_kind", "VARCHAR"),
]


import json  # noqa: E402


def flatten_run(run: dict) -> dict:
    """Fold the nested run manifest into the flat benchmark_runs row."""
    host = run.get("host", {})
    calibration = run.get("calibration", {})
    lines = run.get("lines", [])

    return {
        "run_id": run["run_id"],
        "run_date": run["run_date"],
        "finished": run.get("finished"),
        "status": run.get("status"),
        "engine_version": run.get("engine_version"),
        "engine_build": run.get("engine_build"),
        "git_sha": run.get("git_sha"),
        "git_dirty": run.get("git_dirty"),
        "engine_path": run.get("engine_path"),
        "python_version": host.get("python_version"),
        "gil_enabled": host.get("gil_enabled"),
        "allocator_preload": run.get("allocator_preload"),
        "peak_rss_reset_supported": host.get("peak_rss_reset_supported"),
        "instance_type": run.get("instance_type"),
        "availability_zone": run.get("availability_zone"),
        "storage": run.get("storage"),
        "kernel": host.get("kernel"),
        "cpu_model": host.get("cpu_model"),
        "cpu_count": host.get("cpu_count"),
        "mem_total_bytes": host.get("mem_total_bytes"),
        "calibration_open_ms": calibration.get("open_ms"),
        "calibration_close_ms": calibration.get("close_ms"),
        "calibration_drift": calibration.get("drift"),
        "corpus_hashes": json.dumps(run.get("corpus_hashes", {}), sort_keys=True),
        "suite_wall_ms": sum(line.get("wall_ms") or 0 for line in lines) or None,
    }
