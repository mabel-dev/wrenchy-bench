"""The query driver. One coordinator process per line, invoked by run_suite.py.

The coordinator isolates each query in its own subprocess, hard-killed from
the outside if it runs away — see run_query_isolated() for why. A query that
blows up gets recorded with status="killed" rather than costing the rest of
the line's data.

This repo runs its own driver rather than shelling out to opteryx-core's
`tests/performance/*/runner.py`. Those runners are the record of how the
historical numbers were produced and should keep working unchanged; they are
also four different drivers with four output shapes, three of which record no
result column count and none of which record resource or engine telemetry.
Owning the driver means one protocol across all seven lines, and every column
in `opteryx.benchmarks.telemetry` populated from the first run.

Query sets are vendored under `queries/` — a benchmark repo owns its queries.

Emits normalised JSONL on stdout; progress on stderr.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from harness.config import SUITE_BY_ID  # noqa: E402
from harness.probe import Probe  # noqa: E402

# Headroom over a line's own timeout_s for the hard, external kill of an
# isolated query subprocess. A query that legitimately times out breaks its
# own iteration loop well inside timeout_s; this margin only needs to cover
# process/interpreter startup.
HARD_KILL_MARGIN_S = 60


def bind_engine():
    """Import the installed opteryx and report exactly which one it is.

    The suite measures the PUBLISHED WHEEL. opteryx-core releases several times
    a week, so a wheel tracks development finer than a weekly benchmark can
    resolve, and it measures what users actually get rather than whatever main
    happened to be at 02:00 on a Sunday.

    The resolved path is printed and recorded, not asserted: an editable
    checkout on the path is a legitimate way to run this locally, but a run
    that cannot say which engine produced its numbers is not.
    """
    import opteryx

    print(
        f"engine: {opteryx.__version__}+{opteryx.__build__} from {opteryx.__file__}",
        file=sys.stderr,
    )
    return opteryx


QUERY_ROOT = os.path.join(os.path.dirname(HERE), "queries")

# The 21 IMDB tables. Rewriting is scoped to the FROM clause and anchored on
# `<table> AS`, so a table name reused as a column reference or an output alias
# (`AS movie_keyword`, `mi.info`) is never clobbered.
JOB_TABLES = (
    "aka_name", "aka_title", "cast_info", "char_name", "comp_cast_type",
    "company_name", "company_type", "complete_cast", "info_type", "keyword",
    "kind_type", "link_type", "movie_companies", "movie_info", "movie_info_idx",
    "movie_keyword", "movie_link", "name", "person_info", "role_type", "title",
)

H2O_TABLES = ("x", "small", "medium", "big")


# --------------------------------------------------------------------------
# Query loading
# --------------------------------------------------------------------------


def _read(path: str) -> str:
    with open(path) as handle:
        return handle.read().strip()


def load_queries(line) -> list[tuple[str, str]]:
    """[(name, sql)] in canonical order, with table names bound to the dataset."""
    benchmark = line.benchmark
    directory = os.path.join(QUERY_ROOT, benchmark)
    dataset = line.relation

    if benchmark == "tpch":
        queries = []
        for path in sorted(os.listdir(directory)):
            if not path.endswith(".sql"):
                continue
            number = int(re.sub(r"\D", "", path))
            sql = _read(os.path.join(directory, path))
            # The vendored files carry the upstream placeholder prefixes.
            sql = sql.replace("testdata.tpch_tiny.", f"{dataset}.")
            sql = sql.replace("testdata.tpch.", f"{dataset}.")
            queries.append((f"q{number:02d}", sql))
        return queries

    if benchmark == "job":
        queries = []
        for path in sorted(os.listdir(directory), key=_job_sort_key):
            if not path.endswith(".sql"):
                continue
            sql = _read(os.path.join(directory, path))
            head, sep, tail = sql.partition("WHERE")
            for table in JOB_TABLES:
                head = re.sub(
                    rf"(?<![\w.]){table}\s+AS\b",
                    f"{dataset}.{table} AS",
                    head,
                )
            queries.append((path[:-4], head + sep + tail))
        return queries

    if benchmark == "h2o":
        queries = []
        for path in sorted(os.listdir(directory), key=_h2o_sort_key):
            if not path.endswith(".sql"):
                continue
            sql = _read(os.path.join(directory, path))
            # ⛔ The two H2O workloads have DIFFERENT schemas behind the same
            # bare name. `g*.sql` say `FROM x` but mean the groupby table,
            # stored as `x_groupby` (id1..id3 VARCHAR, id4..id6/v1/v2 INT,
            # v3 DOUBLE); `j*.sql` say `FROM x` and mean the join table
            # (id1..id3 INT, id4..id6 VARCHAR, v1 DOUBLE). Binding `x` the same
            # way for both would point the ten groupby queries at the join
            # table — a schema that resolves far enough to produce numbers for
            # a benchmark nobody ran.
            if path.startswith("g"):
                sql = re.sub(
                    rf"\b(FROM|JOIN)\s+x\b", rf"\1 {dataset}.x_groupby", sql, flags=re.IGNORECASE
                )
            else:
                # `FROM x a JOIN small b ON ...` — bare table then a bare alias,
                # so anchor on the FROM/JOIN keyword rather than on `AS`.
                sql = re.sub(
                    rf"\b(FROM|JOIN)\s+({'|'.join(H2O_TABLES)})\b",
                    rf"\1 {dataset}.\2",
                    sql,
                    flags=re.IGNORECASE,
                )
            queries.append((path[:-4], sql))
        return queries

    if benchmark == "clickbench":
        queries = []
        for path in sorted(os.listdir(directory)):
            if not path.endswith(".sql"):
                continue
            queries.append((path[:-4], _read(os.path.join(directory, path)).format(DATASET=dataset)))
        return queries

    raise ValueError(f"no query loader for benchmark {benchmark!r}")


def _job_sort_key(name: str) -> tuple[int, str]:
    """1a, 1b, 1c, 2a, … 33c — numeric first, then the letter."""
    match = re.match(r"(\d+)([a-z]+)", name)
    return (int(match.group(1)), match.group(2)) if match else (999, name)


def _h2o_sort_key(name: str) -> tuple[int, int]:
    """g1..g10 then j1..j5, numerically rather than lexically."""
    match = re.match(r"([gj])(\d+)", name)
    return (0 if match.group(1) == "g" else 1, int(match.group(2))) if match else (9, 0)


# --------------------------------------------------------------------------
# Engine telemetry
# --------------------------------------------------------------------------


def telemetry_columns(readings: dict) -> dict:
    """Pull the columns we record out of Session.telemetry.as_dict().

    The scan counters (rows_read, blobs_read, columns_read, …) are deliberately
    popped from the top level by the engine and surfaced through the per-node
    `operations` breakdown, so they are summed across scan nodes here. Anything
    absent stays None — a missing measurement must stay distinguishable from a
    measurement of zero.
    """
    columns = {
        "bytes_processed": readings.get("bytes_processed"),
        # telemetry reports time_* in seconds; the table is milliseconds.
        "time_planning_ms": (
            readings["time_planning"] * 1000.0 if readings.get("time_planning") else None
        ),
        "blobs_seen": None,
        "blobs_read": None,
        "rows_seen": None,
        "rows_read": None,
        "columns_read": None,
    }

    operations = readings.get("operations") or {}
    scan_totals: dict[str, int] = {}
    for node in operations.values():
        if not isinstance(node, dict):
            continue
        for key in ("blobs_seen", "blobs_read", "rows_seen", "rows_read", "columns_read"):
            value = node.get(key)
            if isinstance(value, (int, float)):
                scan_totals[key] = scan_totals.get(key, 0) + int(value)
    columns.update(scan_totals)
    return columns


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


def run_one(opteryx, sql: str, timeout_s: float) -> dict:
    """Run one query to completion, or until the deadline.

    The timeout is enforced between morsels: it bounds the drain loop, and
    cannot interrupt a single blocking native call. That is honest about what
    it can do — a query reported as `timeout` genuinely exceeded the budget,
    and one reported `ok` genuinely finished.
    """
    gc.collect()
    session = opteryx.session()
    deadline = time.monotonic() + timeout_s
    rows = 0
    columns = None

    with Probe() as probe:
        try:
            for morsel in session.execute_to_morsels(sql):
                if morsel is None:
                    continue
                rows += morsel.num_rows
                if columns is None:
                    columns = morsel.num_columns
                if time.monotonic() > deadline:
                    return {
                        "status": "timeout",
                        "duration_ms": probe_elapsed(probe),
                        "row_count": None,
                        "column_count": None,
                        "error": f"exceeded {timeout_s:.0f}s",
                        **blank_engine_telemetry(),
                    }
        except Exception as exception:  # noqa: BLE001 — a failing query is data, not a crash
            return {
                "status": "error",
                "duration_ms": probe_elapsed(probe),
                "row_count": None,
                "column_count": None,
                "error": f"{type(exception).__name__}: {exception}"[:500],
                **blank_engine_telemetry(),
            }

    record = {
        "status": "ok",
        "duration_ms": probe.wall_ms,
        "row_count": rows,
        # A query returning no morsels has no observable width. Zero rows and
        # zero columns are different facts and are recorded differently.
        "column_count": columns,
        "error": None,
        **telemetry_columns(session.telemetry),
    }
    record.update(probe.reading.as_dict(), cpu_efficiency=probe.cpu_efficiency)
    return record


def probe_elapsed(probe: Probe) -> float:
    return (time.monotonic_ns() - probe._t0) / 1e6


def blank_engine_telemetry() -> dict:
    return {
        key: None
        for key in (
            "bytes_processed", "time_planning_ms", "blobs_seen", "blobs_read",
            "rows_seen", "rows_read", "columns_read", "peak_rss_bytes", "cpu_ms",
            "cpu_efficiency", "disk_read_bytes", "major_faults",
            "involuntary_ctx_switches",
        )
    }


def run_query_isolated(
    line, name: str, ordinal: int, run: dict, out_dir: str
) -> list[dict]:
    """Run one query in its own subprocess, hard-killed from the outside.

    The in-process timeout in run_one() cannot fire for a query stuck inside a
    single blocking native call, and — confirmed 2026-08-17 by reproducing the
    incident on real hardware — cannot fire AT ALL when the query drives the
    box into severe memory pressure: a query that consumed ~31.4GB of a 32GB
    c8g.4xlarge collapsed page cache and left the OS itself unable to run a
    trivial `free -m` for 18+ minutes, with no OOM-kill and no application-level
    progress to check a deadline against. An external SIGKILL is the only
    backstop proven to still land in that state — the earlier stuck run was
    only ever recovered by a manual `terminate-instances`.

    Isolating each query in its own subprocess also contains the blast radius:
    one query dying does not cost the other 21 queries' worth of real data,
    which a single long-lived process for the whole line would.
    """
    tmp_path = os.path.join(out_dir, f".{line.id}.{name}.tmp.jsonl")
    argv = [
        sys.executable,
        os.path.abspath(__file__),
        "--line", line.id,
        "--run", json.dumps(run),
        "--out", tmp_path,
        "--filter", f"^{name}$",
    ]
    hard_bound = line.timeout_s + HARD_KILL_MARGIN_S

    t0 = time.monotonic()
    killed_reason = None
    try:
        completed = subprocess.run(argv, timeout=hard_bound)
        if completed.returncode != 0:
            killed_reason = f"query subprocess exited {completed.returncode}"
    except subprocess.TimeoutExpired:
        killed_reason = f"query subprocess exceeded the {hard_bound:.0f}s hard bound and was killed"
    elapsed_s = time.monotonic() - t0

    records: list[dict] = []
    if os.path.exists(tmp_path):
        with open(tmp_path) as handle:
            for entry in handle:
                if not entry.strip():
                    continue
                try:
                    records.append(json.loads(entry))
                except json.JSONDecodeError:
                    # A SIGKILL lands between the write() and the next flush()
                    # at worst once, on the last line — truncated JSON here is
                    # the kill itself, not a new failure to report.
                    continue
        os.remove(tmp_path)

    # The worker only ever sees the one query it was filtered to, so its own
    # ordinal is always 1 — overwrite with this query's real position in line.
    for record in records:
        record["query_ordinal"] = ordinal

    if killed_reason:
        next_iteration = len(records) + 1
        if next_iteration <= line.iterations:
            record = dict(
                run_id=run["run_id"],
                line=line.id,
                run_date=run["run_date"],
                engine_version=run["engine_version"],
                engine_build=run["engine_build"],
                benchmark=line.benchmark,
                scale_factor=line.scale_factor,
                data_format=line.data_format,
                codec=line.codec,
            )
            record.update(
                query=name,
                query_ordinal=ordinal,
                iteration=next_iteration,
                cache_state="cold" if next_iteration == 1 else "warm",
                status="killed",
                duration_ms=None,
                row_count=None,
                column_count=None,
                error=f"{killed_reason} ({elapsed_s:.0f}s elapsed)"[:500],
                **blank_engine_telemetry(),
            )
            records.append(record)
        print(f"  {name:<6} killed    {killed_reason}", file=sys.stderr)

    return records


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one benchmark line")
    parser.add_argument("--line", required=True, choices=sorted(SUITE_BY_ID))
    parser.add_argument("--run", required=True, help="JSON run context (run_id, engine_version, …)")
    parser.add_argument("--out", required=True, help="JSONL destination")
    parser.add_argument("--filter", default=None, help="regex over query names")
    args = parser.parse_args()

    line = SUITE_BY_ID[args.line]
    run = json.loads(args.run)
    queries = load_queries(line)
    if args.filter:
        pattern = re.compile(args.filter)
        queries = [(name, sql) for name, sql in queries if pattern.search(name)]
    if not queries:
        print(f"no queries matched for {line.id}", file=sys.stderr)
        return 1

    print(
        f"{line.label}: {len(queries)} queries x {line.iterations} iterations, "
        f"{line.timeout_s:.0f}s timeout",
        file=sys.stderr,
    )

    if args.filter is None:
        # Coordinator: isolate each query in its own subprocess, hard-killed
        # from the outside. See run_query_isolated() for why the in-process
        # timeout below is not, on its own, a sufficient backstop.
        out_dir = os.path.dirname(os.path.abspath(args.out)) or "."
        os.makedirs(out_dir, exist_ok=True)
        with open(args.out, "w") as sink:
            for ordinal, (name, _) in enumerate(queries, start=1):
                for record in run_query_isolated(line, name, ordinal, run, out_dir):
                    sink.write(json.dumps(record) + "\n")
                sink.flush()
        return 0

    # Worker: run the (already narrowed-down) queries in-process. Invoked by
    # the coordinator above with --filter set to exactly one query name; also
    # usable directly for local debugging against a subset of queries.
    opteryx = bind_engine()
    base = {
        "run_id": run["run_id"],
        "line": line.id,
        "run_date": run["run_date"],
        "engine_version": run["engine_version"],
        "engine_build": run["engine_build"],
        "benchmark": line.benchmark,
        "scale_factor": line.scale_factor,
        "data_format": line.data_format,
        "codec": line.codec,
    }

    with open(args.out, "w") as sink:
        for ordinal, (name, sql) in enumerate(queries, start=1):
            for iteration in range(1, line.iterations + 1):
                result = run_one(opteryx, sql, line.timeout_s)
                record = dict(base)
                record.update(
                    query=name,
                    query_ordinal=ordinal,
                    iteration=iteration,
                    # Iteration 1 runs against a page cache dropped before the
                    # line; 2+ are warm. Recorded rather than derived, because
                    # the protocol is a property of the run.
                    cache_state="cold" if iteration == 1 else "warm",
                    **result,
                )
                sink.write(json.dumps(record) + "\n")
                sink.flush()

                if result["status"] == "timeout":
                    # Repeating a timeout buys no information and costs the
                    # full timeout again.
                    break

            print(
                f"  {name:<6} {result['status']:<8} "
                f"{(result['duration_ms'] or 0):>9.1f}ms  "
                f"{(result['row_count'] if result['row_count'] is not None else '-'):>10}r",
                file=sys.stderr,
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
