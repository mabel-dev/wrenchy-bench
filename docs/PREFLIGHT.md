# What this depends on in `opteryx-core`

Much less than it used to. This repo drives its own queries through
`harness/bench_runner.py` rather than shelling out to
`tests/performance/*/runner.py`, so the defects that would otherwise have
blocked the first run are simply not in the path any more:

| Was blocking | Now |
|---|---|
| `VARIANT_DATASETS[""]` resolves the ClickBench *parquet* line to the skene mirror | We resolve datasets ourselves; the two ClickBench lines are asserted to bind to different relations |
| JOB's 113 × 2 × 300 s worst case | We own the loop: 3 iterations, 120 s, and no second attempt after a timeout |
| Three of four runners record no result `column_count` | Every line records `row_count` and `column_count` |
| No resource or engine telemetry anywhere | `Probe` + `Session.telemetry` on every query |

Those runners are the record of how the historical numbers were produced and
are left alone.

## Still needed

### 1. Skene mirrors for JOB and H2O

Built once, out of band, by `corpus/convert.py` — which calls opteryx-core's
own `dev/parquet_to_skene.py`. No engine change, but it must happen before the
first run:

```bash
python corpus/convert.py --checkout ../opteryx-core --all
```

`lz4` is not optional; it is the performance posture every other Skene mirror
uses, and dropping it writes an uncompressed mirror into the same path that
nothing downstream would report.

### 2. `make job` / `make h2o` should run on Skene too

So that a number quoted from a developer laptop and a number from the weekly
run are the same measurement. This needs `--variant` support in
`tests/performance/{job,h2o}/run.py` — the dataset prefix is currently the
constant `testdata.job.` / `testdata.h2o.<size>.` — plus the mirror-generation
stanza in the Makefile targets, mirroring what `tpch` and `clickbench-skene`
already do. H2O's default size moves to `medium` and `small` is dropped.

**Tracked as a companion PR against `opteryx-core`.** The weekly suite does not
depend on it; it exists so local and CI numbers stay comparable.

### 3. A missing allocator preload should be a hard failure

`BENCH_PRELOAD` resolves mimalloc via `draken.preload_library_path()` and, per
its own comment, falls back to *no preload* if the allocator is not found — the
target still runs.

`harness/run_suite.py` already refuses to start in that state, so the weekly run
is covered. A developer running `make tpch` locally still gets the silent
version, and that is where a misleading number gets quoted from.

## Deliberately not required

**Free-threaded Python.** The suite pins stock CPython 3.14. Execution is native
and already runs with the GIL released, so 3.14t bought nothing; opteryx-core
also stopped publishing `cp314t` wheels after 0.9.16, and installing onto 3.14t
falls through to the sdist and tries to build Rust on the box.
