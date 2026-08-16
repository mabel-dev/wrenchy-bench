# Preflight: changes needed in `opteryx-core`

Four of these change what the numbers *mean*. None is more than a few lines,
and all of them are worth landing **before** the first weekly run — a suite
built on top of them unfixed produces a year of history that has to be thrown
away.

They live in `opteryx-core`, so they are the architect's call, not this repo's.

---

## 1. The ClickBench parquet line currently runs Skene

`tests/performance/clickbench/opteryx/runner.py`:

```python
DATASET = Dataset.FULL_SPLIT_SKENE
VARIANT_DATASETS = {
    "": DATASET,                          # <- resolves to the skene mirror
    "skene": Dataset.FULL_SPLIT_SKENE,
}
```

The no-variant run — the one that is supposed to be the parquet-partitioned
line — resolves to `scratch/hits_skene`. Both ClickBench rows in this suite
would report the same dataset under two names.

This is the same class of bug the file's own comment block records from
2026-08-14, when `make clickbench-skene` announced Skene and benchmarked
parquet.

**Fix:** point `""` at `FULL_SPLIT_RUGO_262K`, and add an explicit `parquet`
key so the suite can name the format rather than rely on a default. The suite
passes `--variant parquet`; today that raises a `KeyError`, which is the
correct failure — better than a silent one — but it does mean the ClickBench
parquet line cannot run until this lands.

Worth adding alongside: assert the two ClickBench runs in a suite resolved to
*different* paths, and print the resolved path in the results header.

---

## 2. JOB's worst case is longer than the whole run budget

113 queries × 2 iterations × the 300 s default timeout is **18.8 hours** if
everything times out — and the JOB README says outright that some queries are
expected to. Even at 120 s it is 7.5 h against a 4–5 h target.

**Fix, two parts:**

- The suite already passes `--timeout 60`. A query crossing 60 s is a
  regression whether or not it would have finished at 300 s, so the bound
  loses nothing the trend needs.
- **Skip iteration 2 for any query that timed out on iteration 1.** Repeating a
  timeout buys no information and costs the full timeout again. This halves the
  worst case and needs about three lines in the iteration loop.

Track `timeout_count` as a headline metric so the bound can never hide a
regression. The suite already records it per line.

---

## 3. A missing allocator preload changes every number without saying so

`Makefile`:

```make
BENCH_PRELOAD = LD_PRELOAD=$(shell $(PYTHON) -c 'import draken; print(draken.preload_library_path() or "")' 2>/dev/null) MIMALLOC_PURGE_DELAY=100
```

Per its own comment: *"Empty (allocator not found) => no preload; the target
still runs."* On a box that exists only to produce comparable numbers, that is
a week of results that cannot be compared to any other week.

**Fix:** hard-fail when it resolves empty. `harness/run_suite.py` already does
this on its own side (`resolve_preload`) and refuses to start, so the suite is
covered — but a developer running `make tpch` locally still gets the silent
version, and that is where the misleading number gets quoted from.

---

## 4. TPC-H SF1 has no target and no Skene mirror

SF1 is both a suite line and the calibration bookend — run first and last, one
minute each, and the only cheap way to tell whether the machine you finished on
is the machine you started on.

**Fix:** a `tpch-sf1` target mirroring `tpch-sf100`'s shape, and one build of
`testdata/tpch_1_skene` from the existing `testdata/tpch_1` parquet:

```
python dev/parquet_to_skene.py testdata/tpch_1 testdata/tpch_1_skene lz4
```

`lz4` is not optional — it is the performance posture the other Skene mirrors
use, and dropping it writes an uncompressed mirror that nothing downstream
would report.

---

## 5. `column_count` is recorded by one runner out of four

| runner | records a column count |
|---|---|
| tpch | yes — `col_count` |
| job | no |
| h2o | no |
| clickbench | no (the JSON payload has no row count either) |

ClickBench is the notable gap: its payload records timings only, so neither
`row_count` nor `column_count` reaches the results table. A benchmark that
does not check the shape of its answers can report a fast wrong answer as a
win — which is exactly what TPC-H's own DuckDB shape comparison exists to
prevent.

**Fix:** add `col_count` to the JOB and H2O CSV headers, and `row_count` /
`col_count` to the ClickBench JSON payload.

---

## 6. Per-query telemetry is measured and then discarded

`Session.telemetry` is per-query and always on. It already carries
`bytes_processed` (bytes actually fetched from storage — the only honest
Skene-vs-Parquet number, since it is codec-inclusive and time-independent),
the scan-node `blobs_seen`/`blobs_read`/`rows_seen`/`rows_read`/`columns_read`
readings behind the `operations` breakdown, and the `time_*` phase timings.
None of it reaches a results file.

**Fix:** vendor `harness/probe.py` into `tests/performance/` — it is stdlib
only, so it does not touch the zero-dependency rule — and wrap each query:

```python
with Probe() as p:
    ...
row.update(p.reading.as_dict(), cpu_efficiency=p.cpu_efficiency)
row.update(telemetry_columns(session.telemetry))
```

That gives peak RSS, CPU efficiency, block-device reads, major faults and the
engine's own counters on every row. None of it can be backfilled onto a run
that has already happened.

**Do not enable per-operator profiling in the timed pass.** The ClickBench
runner already gets this right — `--profile` drives a *separate* `EXPLAIN
ANALYZE` pass so the benchmark numbers stay tracing-free. Keep that
separation; operator rows land in `benchmark_operations` tagged with their
pass kind, and never mix into a timing trend.
