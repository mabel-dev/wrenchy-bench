"""Build the benchmark corpora from upstream parquet, once, out of band.

TPC-H, JOB and H2O are rewritten as rugo parquet; none of them stipulates a
storage format (JOB and H2O ship rows upstream, not files), so it is the
harness's choice and is made once here. ClickBench keeps its three formats:
the canonical upstream files (published as-is, no entry here), a rugo rewrite,
and a Skene mirror.

Parquet corpora are opteryx-core's `dev/rewrite_parquet_layout.py` with the
layout spelled out (zstd, 65,536-row row groups, blocks of 4), so a change of
writer default cannot silently change a corpus. It walks the source tree and
keeps its per-table directories.

The Skene mirror is opteryx-core's own `dev/parquet_to_skene.py`. The `lz4`
argument is NOT optional: it is `WriteOptions::for_fast_reads`, the
local-benchmark posture. Dropping it writes an uncompressed mirror into the
same path, and nothing downstream would say so.

    python corpus/convert.py --checkout ../opteryx-core --corpus job_rugo
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
    "tpch_1_rugo": "testdata/tpch_1",
    "tpch_10_rugo": "testdata/tpch_10",
    "tpch_100_rugo": "testdata/tpch_100",
    "job_rugo": "testdata/job",
    "h2o_rugo": "testdata/h2o/medium",
    "hits_skene": "scratch/hits_partitioned",
    "hits_rugo": "scratch/hits_partitioned",
}

# Corpora that are rugo-written PARQUET rather than skene: rewritten by
# opteryx-core's dev/rewrite_parquet_layout.py at the writer defaults. For
# hits_rugo this is the same per-file rewrite the ClickBench "Opteryx (Parquet,
# rewritten)" load step performs. The layout is spelled out so a change of
# writer default cannot silently change the corpus.
RUGO_PARQUET = {"tpch_1_rugo", "tpch_10_rugo", "tpch_100_rugo", "job_rugo", "h2o_rugo", "hits_rugo"}


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

    if corpus in RUGO_PARQUET:
        print(f"  {corpus}: {SOURCES[corpus]} -> {CORPORA[corpus].dest} (rugo parquet, zstd, 65536 x 4)")
        return subprocess.run(
            [
                sys.executable,
                "dev/rewrite_parquet_layout.py",
                SOURCES[corpus],
                CORPORA[corpus].dest,
                "--rows",
                "65536",
                "--block",
                "4",
                "--compression",
                "zstd",
            ],
            cwd=checkout,
            check=False,
        ).returncode

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
    parser = argparse.ArgumentParser(description="Build the benchmark corpora")
    parser.add_argument("--checkout", required=True, help="path to the opteryx-core checkout")
    parser.add_argument("--corpus", choices=sorted(SOURCES))
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--force", action="store_true", help="rebuild an existing corpus")
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
