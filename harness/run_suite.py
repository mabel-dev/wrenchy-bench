"""Drive the seven lines, normalise the output, and write the run bundle.

Runs on the benchmark box, from inside the opteryx-core checkout. Everything it
produces lands under --out:

    manifest.json     run-level provenance -> opteryx.telemetry.benchmark_runs
    results.jsonl     one record per query per iteration -> opteryx.telemetry.benchmarks
    raw/              each runner's native CSV/JSON, verbatim
    console/          per-line stdout+stderr
    STATUS            ok | suspect | failed, written last

Lines run strictly serially. Two benchmarks sharing 16 vCPU measure each other.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import adapters, manifest, probe  # noqa: E402
from harness.config import (  # noqa: E402
    CALIBRATION_LINE,
    CALIBRATION_TOLERANCE,
    CORPORA,
    SUITE,
    SUITE_BY_ID,
)


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
    version = _sh(
        [
            python,
            "-c",
            "import opteryx; print(opteryx.__version__); print(opteryx.__build__)",
        ],
        cwd=checkout,
    ).splitlines()
    if len(version) != 2:
        raise RuntimeError(
            f"could not read opteryx version/build from the checkout at {checkout} — "
            "is the engine compiled?"
        )
    dirty = _sh(["git", "status", "--porcelain"], cwd=checkout)
    return {
        "engine_version": version[0],
        "engine_build": int(version[1]),
        "git_sha": _sh(["git", "rev-parse", "HEAD"], cwd=checkout),
        "git_dirty": bool(dirty),
    }


def verify_corpora(checkout: str) -> dict:
    """Verify every corpus the suite will read, before any of it runs."""
    hashes = {}
    for name, corpus in CORPORA.items():
        root = os.path.join(checkout, corpus.dest)
        if not os.path.isdir(root):
            raise FileNotFoundError(
                f"corpus {name} is missing at {root}. The suite never generates "
                "corpora — they are published artifacts. Sync it and re-run."
            )
        hashes[name] = manifest.verify(root).listing_sha256
    return hashes


def run_line(line, checkout: str, out_dir: str, env: dict, run: dict, python: str):
    """Run one line as a child process and normalise what it wrote."""
    raw_dir = os.path.join(out_dir, "raw")
    console_dir = os.path.join(out_dir, "console")
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(console_dir, exist_ok=True)

    argv = [python, *line.argv]
    if line.json_out:
        argv += ["--json", os.path.join(raw_dir, f"{line.id}.json")]

    cache_dropped = drop_page_cache()
    started_after = time.time()

    console_path = os.path.join(console_dir, f"{line.id}.log")
    print(f"  ▸ {line.label}", flush=True)
    with open(console_path, "w") as console:
        code, reading, wall_ms = probe.probe_child(
            argv, cwd=checkout, env=env, stdout=console, stderr=subprocess.STDOUT
        )

    line_result = {
        "line": line.id,
        "exit_code": code,
        "wall_ms": wall_ms,
        "page_cache_dropped": cache_dropped,
        **reading.as_dict(),
        "cpu_efficiency": (reading.cpu_ms / wall_ms) if reading.cpu_ms and wall_ms else None,
    }

    if code != 0:
        # A non-zero exit is reported, not raised: the remaining lines still
        # carry information, and a partial run beats no run.
        line_result["error"] = f"runner exited {code}; see console/{line.id}.log"
        print(f"    ✗ exit {code} — continuing with the remaining lines", flush=True)
        return [], line_result

    output_path = adapters.resolve_output_path(line, raw_dir, started_after)
    if not line.json_out:
        shutil.copy2(output_path, os.path.join(raw_dir, f"{line.id}.csv"))

    records = adapters.read_line_output(line, run, output_path)
    line_result["record_count"] = len(records)
    line_result["timeout_count"] = sum(1 for r in records if r["status"] == "timeout")
    line_result["error_count"] = sum(1 for r in records if r["status"] == "error")
    print(
        f"    {len(records)} records  {wall_ms / 1000:.1f}s  "
        f"peak {(reading.peak_rss_bytes or 0) / 1e9:.1f}GB  "
        f"{line_result['timeout_count']} timeouts",
        flush=True,
    )
    return records, line_result


def calibration_total(records: list[dict]) -> float:
    """Sum of per-query minimums for the bookend line."""
    best: dict[str, float] = {}
    for record in records:
        if record["status"] != "ok" or not record["duration_ms"]:
            continue
        query = record["query"]
        best[query] = min(best.get(query, float("inf")), record["duration_ms"])
    return sum(best.values())


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the weekly Opteryx benchmark suite")
    parser.add_argument("--checkout", required=True, help="path to the opteryx-core checkout")
    parser.add_argument("--out", required=True, help="directory to write the run bundle into")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--run-id", default=None, help="defaults to the UTC start time")
    parser.add_argument(
        "--only",
        default=None,
        help="comma-separated line ids, for a partial run (never used weekly)",
    )
    parser.add_argument(
        "--skip-calibration",
        action="store_true",
        help="skip the closing bookend (local runs only)",
    )
    args = parser.parse_args()

    checkout = os.path.abspath(args.checkout)
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)

    started = datetime.datetime.now(datetime.timezone.utc)
    run_id = args.run_id or started.strftime("%Y-%m-%dT%H:%MZ")

    print(f"opteryx weekly bench · run {run_id}\n", flush=True)

    identity = engine_identity(checkout, args.python)
    preload = resolve_preload(checkout, args.python)
    corpus_hashes = verify_corpora(checkout)
    facts = probe.host_facts()

    run = {
        "run_id": run_id,
        "run_date": started.isoformat(),
        **identity,
    }

    # Child environment only — every value must be a string.
    env = dict(os.environ)
    env.update(
        LD_PRELOAD=preload,
        MIMALLOC_PURGE_DELAY="100",
    )
    env.pop("OPTERYX_DEBUG", None)

    lines = SUITE
    if args.only:
        lines = [SUITE_BY_ID[i] for i in args.only.split(",")]

    all_records: list[dict] = []
    line_results: list[dict] = []

    for line in lines:
        records, result = run_line(line, checkout, out_dir, env, run, args.python)
        all_records.extend(records)
        line_results.append(result)

    # Closing bookend. If the two SF1 totals disagree, the machine moved under
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
            calibration["drift"] = abs(
                calibration["close_ms"] - calibration["open_ms"]
            ) / calibration["open_ms"]

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

    # STATUS last: it is what the CloudWatch alarm watches, so it must not
    # appear until the bundle beside it is complete.
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
