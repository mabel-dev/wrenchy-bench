"""ONE-OFF, run on the benchmark box: build the hits_rugo corpus and publish it.

The canonical ClickBench files (hits_partitioned, already synced onto the box)
are rewritten through rugo's own writer from the INSTALLED opteryx-core wheel,
one output file per source file — the same rewrite as the ClickBench entry
"Opteryx (Parquet, rewritten)" load step and opteryx-core's
dev/rewrite_parquet_layout.py: zstd, 65,536-row row groups, column-major blocks
of 4, spelled out so a change of writer default cannot change the corpus. Row
counts are checked against every source footer.

Then corpus/publish.py stamps the manifest (generator = the wheel that wrote it)
and syncs it to <CORPUS_PREFIX>/hits_rugo/. bootstrap/user-data.sh only calls
this when that prefix has no MANIFEST.json; once published, every later run
syncs it like any other corpus. Remove this script, its call, and the scoped
PutObject grant in infra/main.tf once the corpus exists.

    python corpus/build_hits_rugo.py <hits_partitioned dir> <dest dir> <corpus prefix>
"""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor

ROWS_PER_ROW_GROUP = 65536
ROW_GROUPS_PER_BLOCK = 4
CODEC = "zstd"
# A worker holds one whole decoded file; 8 keeps 32 GiB comfortable.
WORKERS = 8


def rewrite(task: tuple[str, str]) -> int:
    src, dst = task
    from draken.morsels.morsel import Morsel
    from rugo.parquet import read_metadata, read_parquet, write_parquet

    expected = read_metadata(src).num_rows
    with read_parquet(src) as reader:
        morsels = list(reader)
    morsel = Morsel.combine(morsels) if len(morsels) > 1 else morsels[0]
    data = write_parquet(
        morsel,
        compression=CODEC,
        max_rows_per_row_group=ROWS_PER_ROW_GROUP,
        row_groups_per_block=ROW_GROUPS_PER_BLOCK,
    )
    with open(dst + ".tmp", "wb") as handle:
        handle.write(data)
    os.replace(dst + ".tmp", dst)
    written = read_metadata(dst).num_rows
    if written != expected or morsel.num_rows != expected:
        raise RuntimeError(f"{dst}: wrote {written:,} rows, source holds {expected:,}")
    return written


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__)
        return 1
    src, dst, corpus_prefix = sys.argv[1], sys.argv[2], sys.argv[3].rstrip("/")

    names = sorted(f for f in os.listdir(src) if f.endswith(".parquet"))
    if len(names) != 100:
        raise SystemExit(f"{src} holds {len(names)} parquet files, expected the canonical 100")
    os.makedirs(dst, exist_ok=True)
    if os.listdir(dst):
        raise SystemExit(f"{dst} is not empty — refusing to build over it")

    import opteryx

    print(f"--- building hits_rugo with opteryx-core {opteryx.__version__}+{opteryx.__build__}", flush=True)
    tasks = [(os.path.join(src, n), os.path.join(dst, n)) for n in names]
    with ProcessPoolExecutor(max_workers=WORKERS) as pool:
        rows = sum(pool.map(rewrite, tasks))
    print(f"--- hits_rugo: {len(names)} files, {rows:,} rows", flush=True)

    generator = (
        f"opteryx-core {opteryx.__version__}+{opteryx.__build__} rugo.write_parquet "
        f"{CODEC} rows={ROWS_PER_ROW_GROUP} block={ROW_GROUPS_PER_BLOCK}"
    )
    publish = os.path.join(os.path.dirname(os.path.abspath(__file__)), "publish.py")
    return subprocess.run(
        [
            sys.executable,
            publish,
            "--source",
            dst,
            "--name",
            "hits_rugo",
            "--version",
            os.path.basename(corpus_prefix),
            "--generator",
            generator,
        ],
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
