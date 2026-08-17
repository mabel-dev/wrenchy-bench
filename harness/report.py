"""Fold a run bundle into the committed history, detect regressions, write the site data.

Deliberately compares against the JSON history committed in this repo rather
than querying opteryx.benchmarks.telemetry. The suite must not depend on the
thing it measures: the weeks you most want the numbers are exactly the weeks
something is broken, and regression detection that stops working during an
outage is not regression detection. The telemetry tables are the richer surface
for ad-hoc questions; this is the mechanism that has to keep working.

Writes:
    site/data/runs/<run_id>.json   per-query minimums for one run
    site/data/index.json           every run, newest first, with headline totals
    <bundle>/REPORT.md             the PR body
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.config import (  # noqa: E402
    BASELINE_RUNS,
    IMMEDIATE_RATIO,
    REGRESSION_RATIO,
    SUITE,
)

SUITE_ORDER = {line.id: index for index, line in enumerate(SUITE)}


def line_id(record: dict) -> str:
    """The suite line a record belongs to."""
    return record["line"]


def minimums(records: list[dict]) -> dict[str, dict]:
    """Per (line, query) best time.

    The minimum across iterations, and only over iterations that ran clean: an
    `unstable` query had the machine move under it, so its minimum measures the
    machine rather than the engine and must not become a data point.
    """
    grouped: dict[str, dict[str, dict]] = defaultdict(dict)

    by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for record in records:
        by_key[(line_id(record), record["query"])].append(record)

    for (line, query), rows in by_key.items():
        clean = [r for r in rows if r["status"] == "ok" and r["duration_ms"]]
        statuses = {r["status"] for r in rows}
        entry = {
            "ordinal": rows[0]["query_ordinal"],
            "status": (
                "ok"
                if clean and statuses <= {"ok"}
                else ("timeout" if "timeout" in statuses else
                      "unstable" if "unstable" in statuses else "error")
            ),
            "min_ms": min(r["duration_ms"] for r in clean) if clean else None,
            "row_count": next((r["row_count"] for r in rows if r["row_count"] is not None), None),
            "column_count": next(
                (r["column_count"] for r in rows if r["column_count"] is not None), None
            ),
            "peak_rss_bytes": max(
                (r["peak_rss_bytes"] for r in rows if r["peak_rss_bytes"]), default=None
            ),
            "cpu_efficiency": max(
                (r["cpu_efficiency"] for r in rows if r["cpu_efficiency"]), default=None
            ),
        }
        grouped[line][query] = entry

    return grouped


def load_history(site_data: str) -> list[dict]:
    """Committed run summaries, oldest first."""
    runs_dir = os.path.join(site_data, "runs")
    if not os.path.isdir(runs_dir):
        return []
    history = []
    for name in sorted(os.listdir(runs_dir)):
        if name.endswith(".json"):
            with open(os.path.join(runs_dir, name)) as handle:
                history.append(json.load(handle))
    return sorted(history, key=lambda run: run["run_date"])


def detect(current: dict, history: list[dict]) -> list[dict]:
    """Compare this run's minimums against the trailing baseline.

    Only non-suspect runs form a baseline, and only runs whose corpus hashes
    match: a corpus rebuild moves every number in a line, and reporting that as
    113 simultaneous regressions is noise, not signal.
    """
    usable = [
        run
        for run in history
        if run.get("status") == "ok"
        and run.get("corpus_hashes") == current.get("corpus_hashes")
    ][-BASELINE_RUNS:]

    findings: list[dict] = []

    for line, queries in current["lines"].items():
        for query, entry in queries.items():
            previous = [
                run["lines"][line][query]
                for run in usable
                if line in run["lines"] and query in run["lines"][line]
            ]

            # Status transitions need no statistics and are the signal that
            # matters most — report them on the first occurrence.
            was_ok = [p for p in previous if p["status"] == "ok"]
            if entry["status"] in ("error", "timeout") and was_ok:
                findings.append(
                    {
                        "kind": "failure",
                        "line": line,
                        "query": query,
                        "detail": f"status ok → {entry['status']}",
                    }
                )
                continue

            if entry["status"] != "ok" or entry["min_ms"] is None:
                continue

            baseline_times = [p["min_ms"] for p in was_ok if p["min_ms"]]
            if len(baseline_times) < 2:
                continue

            baseline = statistics.median(baseline_times)
            ratio = entry["min_ms"] / baseline

            if ratio >= IMMEDIATE_RATIO:
                findings.append(
                    {
                        "kind": "regression",
                        "line": line,
                        "query": query,
                        "detail": f"{entry['min_ms']:.0f}ms vs {baseline:.0f}ms baseline",
                        "ratio": ratio,
                    }
                )
            elif ratio >= REGRESSION_RATIO:
                # Needs two consecutive runs. A single point over the softer
                # threshold is inside shared-tenancy noise on this instance
                # class, and alerting on it trains people to ignore the channel.
                prior = previous[-1]["min_ms"] if previous and previous[-1]["min_ms"] else None
                if prior and prior / baseline >= REGRESSION_RATIO:
                    findings.append(
                        {
                            "kind": "regression",
                            "line": line,
                            "query": query,
                            "detail": f"{entry['min_ms']:.0f}ms vs {baseline:.0f}ms baseline (2 runs)",
                            "ratio": ratio,
                        }
                    )

    return sorted(findings, key=lambda f: (-f.get("ratio", 99), f["line"], f["query"]))


def summarise(run: dict, current: dict) -> dict:
    """The compact per-run object the site reads."""
    totals = {}
    for line, queries in current["lines"].items():
        clean = [entry["min_ms"] for entry in queries.values() if entry["min_ms"]]
        totals[line] = {
            "sum_min_ms": sum(clean),
            "queries": len(queries),
            "ok": sum(1 for entry in queries.values() if entry["status"] == "ok"),
            "timeout": sum(1 for entry in queries.values() if entry["status"] == "timeout"),
            "error": sum(1 for entry in queries.values() if entry["status"] == "error"),
            "unstable": sum(1 for entry in queries.values() if entry["status"] == "unstable"),
            "peak_rss_bytes": max(
                (entry["peak_rss_bytes"] for entry in queries.values() if entry["peak_rss_bytes"]),
                default=None,
            ),
        }

    return {
        "run_id": run["run_id"],
        "run_date": run["run_date"],
        "status": run.get("status"),
        "engine_version": run.get("engine_version"),
        "engine_build": run.get("engine_build"),
        "git_sha": run.get("git_sha"),
        "instance_type": run.get("instance_type"),
        "corpus_hashes": run.get("corpus_hashes"),
        "calibration": run.get("calibration"),
        "totals": totals,
        "lines": current["lines"],
    }


def render_markdown(summary: dict, findings: list[dict]) -> str:
    out = [
        f"# Weekly bench · {summary['run_id']}",
        "",
        f"**{summary['status'].upper()}** · "
        f"opteryx {summary['engine_version']}+{summary['engine_build']} · "
        f"`{(summary.get('git_sha') or '')[:8]}` · {summary.get('instance_type') or 'unknown host'}",
        "",
    ]

    drift = (summary.get("calibration") or {}).get("drift")
    if drift is not None:
        note = " — machine moved during the run, excluded from baselines" if drift > 0.10 else ""
        out += [f"Calibration drift: {drift:.1%}{note}", ""]

    out += ["| Line | Σ min | queries | ok | timeout | error | peak RSS |", "|---|--:|--:|--:|--:|--:|--:|"]
    for line in sorted(summary["totals"], key=lambda x: SUITE_ORDER.get(x, 99)):
        total = summary["totals"][line]
        peak = total["peak_rss_bytes"]
        out.append(
            f"| {line} | {total['sum_min_ms'] / 1000:.2f}s | {total['queries']} | "
            f"{total['ok']} | {total['timeout']} | {total['error']} | "
            f"{f'{peak / 1e9:.1f} GB' if peak else '—'} |"
        )

    out += ["", "## Findings", ""]
    if not findings:
        out.append("None — no status transitions and nothing over threshold.")
    else:
        for finding in findings:
            marker = "🔴" if finding["kind"] == "failure" else "🟠"
            out.append(f"- {marker} **{finding['line']} / {finding['query']}** — {finding['detail']}")

    return "\n".join(out) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Report on a run bundle")
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--site-data", default="site/data")
    args = parser.parse_args()

    with open(os.path.join(args.bundle, "manifest.json")) as handle:
        run = json.load(handle)
    with open(os.path.join(args.bundle, "results.jsonl")) as handle:
        records = [json.loads(line) for line in handle if line.strip()]

    current = {"lines": minimums(records), "corpus_hashes": run.get("corpus_hashes")}
    history = load_history(args.site_data)
    findings = detect(current, history)
    summary = summarise(run, current)

    os.makedirs(os.path.join(args.site_data, "runs"), exist_ok=True)
    slug = summary["run_id"].replace(":", "").replace("-", "")
    with open(os.path.join(args.site_data, "runs", f"{slug}.json"), "w") as handle:
        json.dump(summary, handle, indent=1, sort_keys=True)

    index = [
        {
            key: run_summary.get(key)
            for key in ("run_id", "run_date", "status", "engine_version", "engine_build", "totals")
        }
        for run_summary in load_history(args.site_data)
    ]
    index.sort(key=lambda entry: entry["run_date"], reverse=True)
    with open(os.path.join(args.site_data, "index.json"), "w") as handle:
        json.dump(index, handle, indent=1)

    report = render_markdown(summary, findings)
    with open(os.path.join(args.bundle, "REPORT.md"), "w") as handle:
        handle.write(report)
    print(report)

    # Exit code drives the workflow's alert step: a failure is worth waking
    # someone for, a soft regression is worth a PR comment.
    return 2 if any(f["kind"] == "failure" for f in findings) else (1 if findings else 0)


if __name__ == "__main__":
    raise SystemExit(main())
