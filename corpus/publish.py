"""Build a corpus manifest and publish the corpus. Run once per corpus.

The weekly run never generates data. Every corpus is built out-of-band, stamped
with a manifest, and published; the run syncs it and verifies the hash before a
single query executes.

    python corpus/publish.py --source ../opteryx-core/testdata/job_skene --name job_skene

Publishes to `s3://opteryx-bench-corpora/<version>/<corpus>/`, which is what
the benchmark box reads — in-region, so the transfer is free. GCS to EC2 would
be ~$9 a run in egress, more than the compute it feeds.

`--also-gcs` additionally writes `gs://opteryx_data/benchmarks/<version>/…`.
That copy is never read by the runner; it exists so the corpora are queryable
as datasets, because Opteryx has a GCS filesystem and no S3 one. Both copies
come from the same local tree in the same publish run and carry the same
manifest, so they cannot drift.

Rebuilding a corpus means a NEW version prefix, never an overwrite. The old
prefix stays so a comparison across the rebuild is still possible, and the
manifest hash change is what tells the reporter to attribute a trend break to
the data rather than to the engine.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import manifest  # noqa: E402
from harness.config import (  # noqa: E402
    CORPORA,
    CORPUS_VERSION,
    GCS_PREFIX,
    S3_CORPUS_PREFIX,
)


def _require(tool: str, hint: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise SystemExit(f"{tool} is not on PATH — {hint}")
    return path


def push_s3(source: str, destination: str, dry_run: bool) -> None:
    _require("aws", "install the AWS CLI")
    argv = ["aws", "s3", "sync", "--only-show-errors", f"{source}/", destination]
    if dry_run:
        argv.append("--dryrun")
    subprocess.run(argv, check=True)


def push_gcs(source: str, destination: str, dry_run: bool) -> None:
    _require("gcloud", "install the Google Cloud CLI")
    argv = ["gcloud", "storage", "rsync", "--recursive", source, destination]
    if dry_run:
        argv.append("--dry-run")
    subprocess.run(argv, check=True)



def main() -> int:
    parser = argparse.ArgumentParser(description="Publish a corpus")
    parser.add_argument("--source", required=True, help="local corpus directory")
    parser.add_argument("--name", required=True, choices=sorted(CORPORA), help="corpus key")
    parser.add_argument("--version", default=CORPUS_VERSION)
    parser.add_argument("--generator", default="", help="tool + version that produced it")
    parser.add_argument(
        "--also-gcs",
        action="store_true",
        help="additionally write the engine-facing GCS copy (never read by the runner)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    corpus = CORPORA[args.name]
    source = os.path.abspath(args.source)

    built = manifest.build(source, args.name, corpus.codec, args.generator)
    print(
        f"{args.name}: {built.object_count} objects, {built.total_bytes / 1e9:.1f} GB, "
        f"codec {built.codec}, {built.listing_sha256[:16]}…"
    )

    # A corpus an order of magnitude off its declared size is a partial build,
    # not a small one. Publishing it would put a plausible number on a fraction
    # of a dataset, which is the failure mode manifests exist to prevent.
    ratio = built.total_bytes / corpus.approx_bytes
    if not 0.5 <= ratio <= 2.0:
        print(
            f"refusing to publish: {built.total_bytes / 1e9:.1f} GB is {ratio:.2f}x the "
            f"declared ~{corpus.approx_bytes / 1e9:.1f} GB for {args.name}. Either the "
            "build is incomplete or harness/config.py is stale.",
            file=sys.stderr,
        )
        return 1

    if not args.dry_run:
        print(f"wrote {manifest.write(source, built)}")

    s3 = f"{S3_CORPUS_PREFIX}/{args.version}/{args.name}/"
    push_s3(source, s3, args.dry_run)
    print(f"published → {s3}")

    if args.also_gcs:
        gcs = f"{GCS_PREFIX}/{args.version}/{args.name}"
        push_gcs(source, gcs, args.dry_run)
        print(f"engine copy → {gcs}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
