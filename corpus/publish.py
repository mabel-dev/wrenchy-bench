"""Build a corpus manifest and push the corpus to S3. Run once per corpus.

The weekly run never generates data. Every corpus is built out-of-band, stamped
with a manifest, and uploaded to a versioned prefix; the run syncs it and
verifies the hash before a single query executes.

    python corpus/publish.py --source ../opteryx-core/testdata/tpch_10_skene \\
        --name tpch_10_skene --prefix s3://opteryx-bench-corpora/v2026-08

Rebuilding a corpus means a NEW prefix (v2026-09/...), never an overwrite. The
old prefix stays so a comparison across the rebuild is still possible, and the
manifest hash change is what tells the reporter to attribute a trend break to
the data rather than to the engine.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import manifest  # noqa: E402
from harness.config import CORPORA  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish a corpus to S3")
    parser.add_argument("--source", required=True, help="local corpus directory")
    parser.add_argument("--name", required=True, choices=sorted(CORPORA), help="corpus key")
    parser.add_argument("--prefix", required=True, help="s3://bucket/version")
    parser.add_argument("--generator", default="", help="tool + version that produced it")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    corpus = CORPORA[args.name]
    source = os.path.abspath(args.source)

    built = manifest.build(source, args.name, corpus.codec, args.generator)
    print(
        f"{args.name}: {built.object_count} objects, "
        f"{built.total_bytes / 1e9:.1f} GB, {built.listing_sha256[:16]}…"
    )

    # A corpus an order of magnitude off its declared size is a partial build,
    # not a small one. Publishing it would put a plausible number on a fraction
    # of a dataset, which is the failure mode manifests exist to prevent.
    ratio = built.total_bytes / corpus.approx_bytes
    if not 0.5 <= ratio <= 2.0:
        print(
            f"refusing to publish: {built.total_bytes / 1e9:.1f} GB is {ratio:.2f}x the "
            f"declared ~{corpus.approx_bytes / 1e9:.1f} GB for {args.name}. "
            "Either the build is incomplete or config.py is stale.",
            file=sys.stderr,
        )
        return 1

    if args.dry_run:
        print("dry run — manifest not written, nothing uploaded")
        return 0

    path = manifest.write(source, built)
    print(f"wrote {path}")

    destination = f"{args.prefix.rstrip('/')}/{args.name}/"
    subprocess.run(
        ["aws", "s3", "sync", "--only-show-errors", f"{source}/", destination],
        check=True,
    )
    print(f"published → {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
