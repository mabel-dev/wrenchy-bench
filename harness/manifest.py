"""Corpus manifests: build one at publish time, verify it on every read.

A trend line is only attributable to the engine if the data underneath it did
not move. Each corpus carries a MANIFEST.json recording its object count, total
bytes and a hash over the (name, size) of every file; each run records the hash
it actually read. When the hash changes, a trend break is attributed to the
corpus rather than reported as an engine regression.

The hash covers names and sizes, not content. Hashing 40GB of SF100 on every
run would cost more than the benchmark it protects, and a same-name same-size
different-content substitution is not a failure mode this system has — the
corpora are write-once objects in a versioned prefix.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass

MANIFEST_NAME = "MANIFEST.json"


@dataclass
class Manifest:
    name: str
    codec: str
    object_count: int
    total_bytes: int
    listing_sha256: str
    generator: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def _walk(root: str) -> list[tuple[str, int]]:
    entries: list[tuple[str, int]] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            if filename == MANIFEST_NAME:
                continue
            full = os.path.join(dirpath, filename)
            entries.append((os.path.relpath(full, root), os.path.getsize(full)))
    entries.sort()
    return entries


def build(root: str, name: str, codec: str, generator: str = "") -> Manifest:
    entries = _walk(root)
    if not entries:
        raise FileNotFoundError(f"{root} contains no files — refusing to publish an empty corpus")

    digest = hashlib.sha256()
    for relpath, size in entries:
        digest.update(f"{relpath}\0{size}\0".encode())

    return Manifest(
        name=name,
        codec=codec,
        object_count=len(entries),
        total_bytes=sum(size for _, size in entries),
        listing_sha256=digest.hexdigest(),
        generator=generator,
    )


def write(root: str, manifest: Manifest) -> str:
    path = os.path.join(root, MANIFEST_NAME)
    with open(path, "w") as handle:
        json.dump(manifest.as_dict(), handle, indent=2, sort_keys=True)
    return path


def load(root: str) -> Manifest:
    with open(os.path.join(root, MANIFEST_NAME)) as handle:
        return Manifest(**json.load(handle))


def verify(root: str) -> Manifest:
    """Re-derive the manifest and compare. Raises on any mismatch.

    Called after the corpus sync and before a single query runs. A partial sync
    that `test -d` would happily accept is the failure this exists to catch —
    benchmarking a fraction of a dataset produces a number, and a fast one.
    """
    stored = load(root)
    actual = build(root, stored.name, stored.codec, stored.generator)

    if actual.listing_sha256 != stored.listing_sha256:
        raise ValueError(
            f"corpus {stored.name} at {root} does not match its manifest:\n"
            f"  objects  stored={stored.object_count} actual={actual.object_count}\n"
            f"  bytes    stored={stored.total_bytes} actual={actual.total_bytes}\n"
            f"  sha256   stored={stored.listing_sha256[:16]}… "
            f"actual={actual.listing_sha256[:16]}…\n"
            "The sync is incomplete or the corpus was modified in place. "
            "This run cannot produce comparable numbers."
        )
    return stored
