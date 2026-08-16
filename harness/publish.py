"""Turn a run bundle into Parquet and commit it to opteryx.telemetry.*

Runs in GitHub Actions, not on the benchmark box: the box's only job is to
produce numbers and put them in S3. Everything downstream — the schema, the
credentials, the retry — lives here where it can be re-run without paying for
another four hours of EC2.

Parquet is written with rugo's own writer (`pip install rugo`), which is
PyArrow- and NumPy-free, so the control plane has the same "no PyArrow"
posture as the engine.

The upload path is the public service: POST /v1/upload/session, PUT each part
(30MB ceiling), POST /commit with the target table and APPEND. It needs a
client-credential pair and nothing else — the in-process catalog route would
mean putting Firestore and GCS keys into CI.

Credentials are a client_id and a PAT, where the PAT IS the client_secret —
authenticate.opteryx.app's token endpoint verifies a PAT first and falls back
to the legacy client record, so both take the same shape. The workflow reads
them from AWS Secrets Manager rather than from GitHub secrets, so the platform
credential has one home and rotating it there is enough.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.config import (  # noqa: E402
    TABLE_QUERIES,
    TABLE_RUNS,
    TELEMETRY_COLLECTION,
    TELEMETRY_WORKSPACE,
)
from harness.schema import QUERY_COLUMNS, RUN_COLUMNS, flatten_run  # noqa: E402

PART_CEILING = 30 * 1024 * 1024  # the service returns 413 above this
AUTH_URL = os.environ.get("OPTERYX_AUTH_URL", "https://authenticate.opteryx.app/v1/token")
UPLOAD_URL = os.environ.get("OPTERYX_UPLOAD_URL", "https://upload.opteryx.app/v1/upload")


# --------------------------------------------------------------------------
# Parquet
# --------------------------------------------------------------------------


def to_parquet(records: list[dict], columns: list[tuple[str, str]]) -> bytes:
    """Serialise records to Parquet with an explicit, pinned column order.

    The column list is the schema contract. A record carrying a key that is not
    in it is a schema drift the table would silently absorb, so it raises here
    instead — the table is append-only and a widened schema cannot be undone
    without a rewrite.
    """
    from draken.draken_native import DrakenType
    from draken.interop.vector_sequence import vector_from_sequence
    from draken.morsels.morsel import Morsel
    from rugo.parquet import write_parquet

    declared = {name for name, _ in columns}
    for record in records:
        unknown = set(record) - declared
        if unknown:
            raise ValueError(
                f"record carries undeclared columns {sorted(unknown)}; add them to "
                "harness/schema.py so the change is reviewed rather than absorbed"
            )

    kinds = {
        "VARCHAR": DrakenType.VARCHAR,
        "INT64": DrakenType.INT64,
        "INT32": DrakenType.INT32,
        "FLOAT64": DrakenType.FLOAT64,
        "BOOLEAN": DrakenType.BOOLEAN,
        "TIMESTAMP": DrakenType.TIMESTAMP,
    }

    names, vectors = [], []
    for name, kind in columns:
        values = [record.get(name) for record in records]
        names.append(name.encode())
        vectors.append(vector_from_sequence(values, kinds[kind]))

    return write_parquet(Morsel.from_vectors(names, vectors), profile="storage")


# --------------------------------------------------------------------------
# Upload service
# --------------------------------------------------------------------------


def _post(url: str, body: dict | None, token: str | None = None) -> dict:
    data = json.dumps(body or {}).encode()
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read() or b"{}")


def get_token(client_id: str, client_secret: str) -> str:
    """Exchange a client_id + PAT for an access token.

    FORM-ENCODED, not JSON. The token endpoint declares its parameters as
    FastAPI `Form(...)`, so a JSON body is rejected with 422 no matter how
    correct the credentials are — the failure looks like an auth problem and
    is not one.
    """
    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
    ).encode()
    request = urllib.request.Request(AUTH_URL, data=body, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read())["access_token"]


def upload_table(token: str, dataset: str, blob: bytes, message: str) -> dict:
    """Session → parts → commit. Appends; never overwrites."""
    session = _post(f"{UPLOAD_URL}/session", {}, token)
    session_id = session["session_id"]

    for part, offset in enumerate(range(0, len(blob), PART_CEILING)):
        chunk = blob[offset : offset + PART_CEILING]
        request = urllib.request.Request(
            f"{UPLOAD_URL}/{session_id}?part={part}", data=chunk, method="PUT"
        )
        request.add_header("Authorization", f"Bearer {token}")
        request.add_header("Content-Type", "application/octet-stream")
        request.add_header("x-file-name", f"{dataset}-{part}.parquet")
        with urllib.request.urlopen(request, timeout=300):
            pass

    return _post(
        f"{UPLOAD_URL}/{session_id}/commit",
        {
            "target": {
                "workspace": TELEMETRY_WORKSPACE,
                "collection": TELEMETRY_COLLECTION,
                "dataset": dataset,
            },
            "snapshot_message": message,
            # APPEND: the tables are an immutable history. A re-publish of the
            # same run_id is de-duplicated downstream rather than by overwriting
            # a snapshot that other rows share.
            "conflict_resolution": "append",
        },
        token,
    )


# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish a run bundle to opteryx.telemetry")
    parser.add_argument("--bundle", required=True, help="directory holding manifest.json + results.jsonl")
    parser.add_argument("--dry-run", action="store_true", help="write parquet locally, do not upload")
    args = parser.parse_args()

    with open(os.path.join(args.bundle, "manifest.json")) as handle:
        run = json.load(handle)
    with open(os.path.join(args.bundle, "results.jsonl")) as handle:
        records = [json.loads(line) for line in handle if line.strip()]

    if not records:
        # An empty bundle is a failed run, not a successful no-op. Committing
        # nothing would leave the trend looking healthy through an outage.
        print("bundle holds no query records — nothing to publish", file=sys.stderr)
        return 1

    queries_blob = to_parquet(records, QUERY_COLUMNS)
    runs_blob = to_parquet([flatten_run(run)], RUN_COLUMNS)

    if args.dry_run:
        for name, blob in ((TABLE_QUERIES, queries_blob), (TABLE_RUNS, runs_blob)):
            path = os.path.join(args.bundle, f"{name}.parquet")
            with open(path, "wb") as handle:
                handle.write(blob)
            print(f"wrote {path} ({len(blob) / 1024:.0f} KB)")
        return 0

    token = get_token(os.environ["OPTERYX_CLIENT_ID"], os.environ["OPTERYX_CLIENT_SECRET"])
    message = f"weekly bench {run['run_id']} ({run['engine_version']}+{run['engine_build']})"

    for dataset, blob in ((TABLE_QUERIES, queries_blob), (TABLE_RUNS, runs_blob)):
        result = upload_table(token, dataset, blob, message)
        print(f"{result['table']}  {result.get('rows_written')} rows  commit {result['commit_id']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
