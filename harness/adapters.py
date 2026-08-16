"""Read each runner's native output; emit one normalised record shape.

The four runners write three different CSV headers and one JSON payload. We
adapt on the way out rather than changing their output formats, because those
formats are the record of how every historical number was produced — rewriting
them retroactively muddies the archive we are trying to build on.

Normalised record (one per query per iteration) — see docs/SCHEMA.md:

    run_id run_date engine_version engine_build benchmark scale_factor
    data_format codec query query_ordinal iteration cache_state duration_ms
    row_count column_count status error
    + peak_rss_bytes cpu_ms cpu_efficiency disk_read_bytes major_faults
      involuntary_ctx_switches bytes_processed blobs_seen blobs_read
      rows_seen rows_read columns_read time_planning_ms

Telemetry columns are None until the runner change (work item 6) lands. None
means "not measured"; it must never be written as 0.
"""

from __future__ import annotations

import csv
import json
import os
from typing import Iterator

from .config import Line

TELEMETRY_FIELDS = (
    "peak_rss_bytes",
    "cpu_ms",
    "cpu_efficiency",
    "disk_read_bytes",
    "major_faults",
    "involuntary_ctx_switches",
    "bytes_processed",
    "blobs_seen",
    "blobs_read",
    "rows_seen",
    "rows_read",
    "columns_read",
    "time_planning_ms",
)


def _blank_telemetry() -> dict:
    return {name: None for name in TELEMETRY_FIELDS}


def _num(value, cast=float):
    """Parse a CSV cell, distinguishing empty from zero."""
    if value is None or value == "":
        return None
    return cast(value)


def _cache_state(iteration: int) -> str:
    """Iteration 1 runs against a dropped page cache; 2+ are warm.

    Recorded explicitly rather than derived at query time, because the protocol
    is a property of the run and may change.
    """
    return "cold" if iteration == 1 else "warm"


def _base(line: Line, run: dict) -> dict:
    return {
        "run_id": run["run_id"],
        "run_date": run["run_date"],
        "engine_version": run["engine_version"],
        "engine_build": run["engine_build"],
        "benchmark": line.benchmark,
        "scale_factor": line.scale_factor,
        "data_format": line.data_format,
        "codec": line.codec,
    }


def _latest_csv(results_dir: str, started_after: float) -> str:
    """The CSV this line just wrote.

    Filtered on mtime rather than just taking the newest file: the runners write
    into a directory that may already hold results from a previous run, and
    picking up a stale CSV would republish last week's numbers under this
    week's run_id.
    """
    candidates = [
        os.path.join(results_dir, name)
        for name in os.listdir(results_dir)
        if name.endswith(".csv")
    ]
    fresh = [path for path in candidates if os.path.getmtime(path) >= started_after]
    if not fresh:
        raise FileNotFoundError(
            f"{results_dir} holds no CSV written after the line started "
            f"({len(candidates)} older files present) — the runner produced no results"
        )
    return max(fresh, key=os.path.getmtime)


# --------------------------------------------------------------------------
# CSV readers
# --------------------------------------------------------------------------


def _read_csv_rows(line: Line, run: dict, path: str, col_count_field: str | None) -> Iterator[dict]:
    base = _base(line, run)
    ordinals: dict[str, int] = {}

    with open(path, newline="") as handle:
        for row in csv.DictReader(handle):
            query = row["query"]
            if query not in ordinals:
                ordinals[query] = len(ordinals) + 1
            iteration = int(row["run"])

            record = dict(base)
            record.update(
                query=query,
                query_ordinal=ordinals[query],
                iteration=iteration,
                cache_state=_cache_state(iteration),
                duration_ms=_num(row.get("elapsed_ms")),
                row_count=_num(row.get("row_count"), int),
                column_count=(
                    _num(row.get(col_count_field), int) if col_count_field else None
                ),
                status=row.get("status") or "ok",
                error=(row.get("error") or None),
            )
            record.update(_blank_telemetry())
            yield record


def read_tpch_csv(line: Line, run: dict, path: str) -> Iterator[dict]:
    # The only runner that already records a result column count.
    return _read_csv_rows(line, run, path, "col_count")


def read_job_csv(line: Line, run: dict, path: str) -> Iterator[dict]:
    return _read_csv_rows(line, run, path, None)


def read_h2o_csv(line: Line, run: dict, path: str) -> Iterator[dict]:
    return _read_csv_rows(line, run, path, None)


# --------------------------------------------------------------------------
# ClickBench JSON
# --------------------------------------------------------------------------


def read_clickbench_json(line: Line, run: dict, path: str) -> Iterator[dict]:
    """Expand the ClickBench payload's per-query times_ms array into rows.

    The payload carries no row or column count — it records timings only — so
    those stay None rather than being invented. That is a real gap in the
    benchmark's correctness coverage and is tracked as such, not smoothed over.
    """
    base = _base(line, run)
    with open(path) as handle:
        payload = json.load(handle)

    for ordinal, entry in enumerate(payload["queries"], start=1):
        failed = entry.get("failed", False)
        times = entry.get("times_ms") or []

        if failed and not times:
            record = dict(base)
            record.update(
                query=entry["query"],
                query_ordinal=ordinal,
                iteration=1,
                cache_state=_cache_state(1),
                duration_ms=None,
                row_count=None,
                column_count=None,
                status="error",
                error="query failed; see raw console log",
            )
            record.update(_blank_telemetry())
            yield record
            continue

        for iteration, elapsed in enumerate(times, start=1):
            record = dict(base)
            record.update(
                query=entry["query"],
                query_ordinal=ordinal,
                iteration=iteration,
                cache_state=_cache_state(iteration),
                duration_ms=float(elapsed),
                row_count=None,
                column_count=None,
                status="error" if failed else "ok",
                error="query failed; see raw console log" if failed else None,
            )
            record.update(_blank_telemetry())
            yield record


READERS = {
    "tpch_csv": read_tpch_csv,
    "job_csv": read_job_csv,
    "h2o_csv": read_h2o_csv,
    "clickbench_json": read_clickbench_json,
}


def read_line_output(line: Line, run: dict, path: str) -> list[dict]:
    """Parse one line's output into normalised records, then flag instability.

    A query whose spread across iterations exceeds UNSTABLE_SPREAD of its own
    minimum has had the machine move under it; its minimum is not a signal, so
    every iteration of it is marked so the comparison can exclude it.
    """
    from .config import UNSTABLE_SPREAD

    records = list(READERS[line.reader](line, run, path))

    by_query: dict[str, list[dict]] = {}
    for record in records:
        by_query.setdefault(record["query"], []).append(record)

    for rows in by_query.values():
        times = [r["duration_ms"] for r in rows if r["status"] == "ok" and r["duration_ms"]]
        if len(times) < 2:
            continue
        lowest = min(times)
        if lowest > 0 and (max(times) - lowest) / lowest > UNSTABLE_SPREAD:
            for row in rows:
                if row["status"] == "ok":
                    row["status"] = "unstable"

    return records


def resolve_output_path(line: Line, raw_dir: str, started_after: float) -> str:
    """Where this line left its results."""
    if line.json_out:
        return os.path.join(raw_dir, f"{line.id}.json")
    assert line.results_dir is not None
    return _latest_csv(line.results_dir, started_after)
