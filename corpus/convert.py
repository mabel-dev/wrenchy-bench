"""Build the Skene mirrors, once, out of band.

Every line in the suite except ClickBench-parquet runs on Skene, including JOB
and H2O — which upstream ship as rows, not files, so the format was always the
harness's choice and is now made once here rather than differing per benchmark.

The conversion is opteryx-core's own `dev/parquet_to_skene.py`. The `lz4`
argument is NOT optional: it is `WriteOptions::for_fast_reads`, the
local-benchmark posture every other Skene mirror uses. Dropping it writes an
uncompressed mirror into the same path, and nothing downstream would say so.

    python corpus/convert.py --checkout ../opteryx-core --corpus job_skene
    python corpus/convert.py --checkout ../opteryx-core --all

H2O is converted at `medium` only. `small` (1e7 rows, 630MB) fits entirely in
page cache on a 32GiB box, so it measures compute with the storage layer
removed; it is not carried into the suite.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.config import CORPORA  # noqa: E402

# corpus -> parquet source under the checkout. ClickBench parquet is a suite
# line in its own right and is published as-is, so it has no entry.
SOURCES = {
    "tpch_1_skene": "testdata/tpch_1",
    "tpch_10_skene": "testdata/tpch_10",
    "tpch_100_skene": "testdata/tpch_100",
    "job_skene": "testdata/job",
    "h2o_skene": "testdata/h2o/medium",
    "hits_skene": "scratch/hits_partitioned",
}


def convert(checkout: str, corpus: str, force: bool) -> int:
    source = os.path.join(checkout, SOURCES[corpus])
    destination = os.path.join(checkout, CORPORA[corpus].dest)

    if not os.path.isdir(source):
        print(f"  {corpus}: source {source} does not exist — skipping", file=sys.stderr)
        return 1

    if os.path.isdir(destination):
        if not force:
            print(f"  {corpus}: {destination} exists (use --force to rebuild)")
            return 0
        # The converter refuses to write over an existing tree, and a partial
        # tree is worse than none: it would pass an `isdir` check and silently
        # benchmark a fraction of the dataset.
        subprocess.run(["rm", "-rf", destination], check=True)

    print(f"  {corpus}: {SOURCES[corpus]} -> {CORPORA[corpus].dest} (lz4)")
    return subprocess.run(
        [
            sys.executable,
            "dev/parquet_to_skene.py",
            SOURCES[corpus],
            CORPORA[corpus].dest,
            "lz4",
        ],
        cwd=checkout,
        check=False,
    ).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the Skene mirrors")
    parser.add_argument("--checkout", required=True, help="path to the opteryx-core checkout")
    parser.add_argument("--corpus", choices=sorted(SOURCES))
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--force", action="store_true", help="rebuild an existing mirror")
    args = parser.parse_args()

    if not args.all and not args.corpus:
        parser.error("pass --corpus <name> or --all")

    targets = sorted(SOURCES) if args.all else [args.corpus]
    checkout = os.path.abspath(args.checkout)

    failures = 0
    for corpus in targets:
        failures += 1 if convert(checkout, corpus, args.force) else 0
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
