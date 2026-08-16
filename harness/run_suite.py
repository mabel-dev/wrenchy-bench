"""Drive the seven lines and write the run bundle.

Runs on the benchmark box. Each line is a separate `bench_runner.py` process —
a fresh interpreter and a fresh allocator arena per line, so one line's
fragmentation cannot be charged to the next. Lines run strictly serially: two
benchmarks sharing 16 vCPU measure each other.

Everything it produces lands under --out:

    manifest.json     run-level provenance -> opteryx.telemetry.benchmark_runs
    results.jsonl     one record per query per iteration -> opteryx.telemetry.benchmarks
    raw/              each line's own JSONL, as the driver wrote it
    console/          per-line stderr
    STATUS            ok | suspect | failed, written last
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import manifest, probe  # noqa: E402
from harness.config import (  # noqa: E402
    CALIBRATION_LINE,
    CALIBRATION_TOLERANCE,
    CORPORA,
    SUITE,
    SUITE_BY_ID,
    UNSTABLE_SPREAD,
)

HARNESS = os.path.dirname(os.path.abspath(__file__))


def _sh(argv: list[str], cwd: str | None = None) -> str:
    return subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, check=False
    ).stdout.strip()


def resolve_preload(checkout: str, python: str) -> str:
    """Resolve the mimalloc preload, and refuse to run without it.

    The Makefile's BENCH_PRELOAD falls back to no preload when the allocator is
    not found and the target still runs. On a box that exists only to produce
    comparable numbers that is a whole week of results that cannot be compared
    to any other week, so here it is a hard stop.
    """
    path = _sh(
        [python, "-c", "import draken; print(draken.preload_library_path() or '')"],
        cwd=checkout,
    )
    if not path or not os.path.exists(path):
        raise RuntimeError(
            "allocator preload did not resolve (draken.preload_library_path() is empty). "
            "Running without it changes every number in the suite and would silently "
            "produce a week of incomparable results."
        )
    return path


def drop_page_cache() -> bool:
    """Drop the page cache before a line, so every line starts the same way.

    Before each LINE, not each query: dropping between queries produces numbers
    dominated by storage that would swamp any engine change. What matters is
    that the choice is fixed and recorded.
    """
    if not sys.platform.startswith("linux"):
        return False
    subprocess.run(["sync"], check=True)
    with open("/proc/sys/vm/drop_caches", "w") as handle:
        handle.write("3\n")
    return True


def engine_identity(checkout: str, python: str) -> dict:
    identity = _sh(
        [python, "-c", "import opteryx; print(opteryx.__version__); print(opteryx.__build__)"],
        cwd=checkout,
    ).splitlines()
    if len(identity) != 2:
        raise RuntimeError(
            f"could not read opteryx version/build from {checkout} — is the engine compiled?"
        )
    return {
        "engine_version": identity[0],
        "engine_build": int(identity[1]),
        "git_sha": _sh(["git", "rev-parse", "HEAD"], cwd=checkout),
        "git_dirty": bool(_sh(["git", "status", "--porcelain"], cwd=checkout)),
    }


def verify_corpora(checkout: str, lines: list) -> dict:
    """Verify every corpus the run will read, before any of it runs."""
    hashes = {}
    for name in sorted({line.corpus for line in lines}):
        root = os.path.join(checkout, CORPORA[name].dest)
        if not os.path.isdir(root):
            raise FileNotFoundError(
                f"corpus {name} is missing at {root}. The suite never generates "
                "corpora — they are published artifacts. Sync it and re-run."
            )
        hashes[name] = manifest.verify(root).listing_sha256
    return hashes


def flag_unstable(records: list[dict]) -> None:
    """Mark queries whose spread across iterations exceeds the threshold.

    An unstable query's minimum is not a signal: the machine moved under it, so
    a change measured against it is measuring the machine.
    """
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


def run_line(line, checkout: str, out_dir: str, env: dict, run: dict, python: str):
    """Run one line in its own process and read back what it wrote."""
    raw_dir = os.path.join(out_dir, "raw")
    console_dir = os.path.join(out_dir, "console")
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(console_dir, exist_ok=True)

    jsonl = os.path.join(raw_dir, f"{line.id}.jsonl")
    argv = [
        python,
        os.path.join(HARNESS, "bench_runner.py"),
        "--line", line.id,
        "--run", json.dumps(run),
        "--out", jsonl,
    ]

    cache_dropped = drop_page_cache()
    print(f"  ▸ {line.label}", flush=True)

    with open(os.path.join(console_dir, f"{line.id}.log"), "w") as console:
        code, reading, wall_ms = probe.probe_child(
            argv, cwd=checkout, env=env, stdout=console, stderr=subprocess.STDOUT
        )

    result = {
        "line": line.id,
        "exit_code": code,
        "wall_ms": wall_ms,
        "page_cache_dropped": cache_dropped,
        "process_peak_rss_bytes": reading.peak_rss_bytes,
    }

    records: list[dict] = []
    if os.path.exists(jsonl):
        with open(jsonl) as handle:
            records = [json.loads(entry) for entry in handle if entry.strip()]
    flag_unstable(records)

    if code != 0:
        # Reported, not raised: a partial run beats no run, and the remaining
        # lines still carry information.
        result["error"] = f"driver exited {code}; see console/{line.id}.log"
        print(f"    ✗ exit {code} — continuing with the remaining lines", flush=True)

    result["record_count"] = len(records)
    result["timeout_count"] = sum(1 for r in records if r["status"] == "timeout")
    result["error_count"] = sum(1 for r in records if r["status"] == "error")
    result["unstable_count"] = sum(1 for r in records if r["status"] == "unstable")

    peak = reading.peak_rss_bytes or 0
    print(
        f"    {len(records)} records  {wall_ms / 1000:.1f}s  peak {peak / 1e9:.1f}GB  "
        f"{result['timeout_count']} timeouts  {result['error_count']} errors",
        flush=True,
    )
    return records, result


def calibration_total(records: list[dict]) -> float:
    """Sum of per-query minimums over the queries that ran clean."""
    best: dict[str, float] = {}
    for record in records:
        if record["status"] != "ok" or not record["duration_ms"]:
            continue
        best[record["query"]] = min(best.get(record["query"], float("inf")), record["duration_ms"])
    return sum(best.values())


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the weekly Opteryx benchmark suite")
    parser.add_argument("--checkout", required=True, help="path to the opteryx-core checkout")
    parser.add_argument("--out", required=True, help="directory to write the run bundle into")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--run-id", default=None, help="defaults to the UTC start time")
    parser.add_argument("--only", default=None, help="comma-separated line ids, for a partial run")
    parser.add_argument("--skip-calibration", action="store_true")
    args = parser.parse_args()

    checkout = os.path.abspath(args.checkout)
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)

    started = datetime.datetime.now(datetime.timezone.utc)
    run_id = args.run_id or started.strftime("%Y-%m-%dT%H:%MZ")
    print(f"wrenchy bench · run {run_id}\n", flush=True)

    lines = [SUITE_BY_ID[i] for i in args.only.split(",")] if args.only else SUITE

    identity = engine_identity(checkout, args.python)
    preload = resolve_preload(checkout, args.python)
    corpus_hashes = verify_corpora(checkout, lines)
    facts = probe.host_facts()

    run = {"run_id": run_id, "run_date": started.isoformat(), **identity}

    env = dict(os.environ)
    env.update(LD_PRELOAD=preload, MIMALLOC_PURGE_DELAY="100")
    env.pop("OPTERYX_DEBUG", None)

    all_records: list[dict] = []
    line_results: list[dict] = []

    for line in lines:
        records, result = run_line(line, checkout, out_dir, env, run, args.python)
        all_records.extend(records)
        line_results.append(result)

    # Closing bookend. If the two SF1 totals disagree the machine moved under
    # the run: every number in it is suspect, so it is kept and reported but
    # excluded from trend baselines.
    calibration = {"open_ms": None, "close_ms": None, "drift": None}
    opening = [r for r in all_records if r["benchmark"] == "tpch" and r["scale_factor"] == "1"]
    if opening:
        calibration["open_ms"] = calibration_total(opening)

    if not args.skip_calibration and calibration["open_ms"]:
        print("  ▸ calibration (closing bookend)", flush=True)
        closing, _ = run_line(
            SUITE_BY_ID[CALIBRATION_LINE], checkout, out_dir, env, run, args.python
        )
        calibration["close_ms"] = calibration_total(closing)
        if calibration["close_ms"]:
            calibration["drift"] = (
                abs(calibration["close_ms"] - calibration["open_ms"]) / calibration["open_ms"]
            )

    failed = any(r["exit_code"] != 0 for r in line_results)
    drifted = (calibration["drift"] or 0) > CALIBRATION_TOLERANCE
    status = "failed" if failed else ("suspect" if drifted else "ok")

    run_manifest = {
        **run,
        "status": status,
        "finished": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "allocator_preload": preload,
        "corpus_hashes": corpus_hashes,
        "calibration": calibration,
        "lines": line_results,
        "host": facts,
        "instance_type": os.environ.get("BENCH_INSTANCE_TYPE", ""),
        "availability_zone": os.environ.get("BENCH_AZ", ""),
        "storage": os.environ.get("BENCH_STORAGE", ""),
    }

    with open(os.path.join(out_dir, "results.jsonl"), "w") as handle:
        for record in all_records:
            handle.write(json.dumps(record) + "\n")

    with open(os.path.join(out_dir, "manifest.json"), "w") as handle:
        json.dump(run_manifest, handle, indent=2, sort_keys=True, default=str)

    # STATUS last: it is what the Action polls and what the alarm watches, so it
    # must not appear before the bundle beside it is complete.
    with open(os.path.join(out_dir, "STATUS"), "w") as handle:
        handle.write(status + "\n")

    print(f"\n{status.upper()}  {len(all_records)} records  → {out_dir}", flush=True)
    if drifted:
        print(
            f"  calibration drift {calibration['drift']:.1%} exceeds "
            f"{CALIBRATION_TOLERANCE:.0%} — the machine moved during this run",
            flush=True,
        )
    return 1 if status == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
