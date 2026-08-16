#!/usr/bin/env bash
#
# Instance bootstrap for the weekly benchmark run. Passed as EC2 user-data by
# the GitHub Actions launcher; templated with the values below.
#
#   ENGINE_REF      git ref of opteryx-core to build and measure
#   HARNESS_REF     git ref of this repo
#   CORPUS_PREFIX   s3://…/v2026-08
#   RESULTS_BUCKET  s3://…
#   RUN_ID          the run identifier the Action is waiting on
#
# The instance does exactly one thing: produce a run bundle and put it in S3.
# It holds no GitHub credentials and no Opteryx credentials — comparison,
# publishing and the site are all downstream in Actions, where they can be
# re-run without paying for another four hours of EC2.

set -Eeuo pipefail

ENGINE_REF="${ENGINE_REF:-main}"
HARNESS_REF="${HARNESS_REF:-main}"
CORPUS_PREFIX="${CORPUS_PREFIX:?}"
RESULTS_BUCKET="${RESULTS_BUCKET:?}"
RUN_ID="${RUN_ID:?}"

WORK=/opt/bench
BUNDLE="${WORK}/bundle"
LOG=/var/log/bench-bootstrap.log

exec > >(tee -a "${LOG}") 2>&1

# ---------------------------------------------------------------------------
# Watchdog FIRST, before anything that could hang.
#
# A benchmark that wedges on a lock or a stalled mount otherwise bills for a
# month. Seven hours is the run budget (4-5h) plus headroom for a slow build.
# ---------------------------------------------------------------------------
shutdown -h +420 "benchmark watchdog" &

# Publish a failure marker if we exit anywhere unexpected. The Action is
# polling for STATUS; without this a crashed run looks identical to a slow one
# until the alarm fires hours later.
on_error() {
    local code=$?
    echo "FAILED at line ${BASH_LINENO[0]} (exit ${code})"
    echo "failed" > /tmp/STATUS
    aws s3 cp /tmp/STATUS "${RESULTS_BUCKET}/runs/${RUN_ID}/STATUS" || true
    aws s3 cp "${LOG}" "${RESULTS_BUCKET}/runs/${RUN_ID}/bootstrap.log" || true
    shutdown -h now
}
trap on_error ERR

echo "=== weekly bench ${RUN_ID} · engine ${ENGINE_REF} · harness ${HARNESS_REF}"

# ---------------------------------------------------------------------------
# Toolchain
# ---------------------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git build-essential clang lld pkg-config libssl-dev curl unzip

curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal
# shellcheck source=/dev/null
source "${HOME}/.cargo/env"

curl -fsSL https://astral.sh/uv/install.sh | sh
export PATH="${HOME}/.local/bin:${PATH}"

# Stock CPython 3.14, NOT the free-threaded 3.14t build. opteryx-core stopped
# publishing cp314t wheels after 0.9.16 and the parallelism target is native
# threads under a released GIL — 3.14t here would silently change what is
# being measured.
uv python install 3.14
mkdir -p "${WORK}" && cd "${WORK}"
uv venv --python 3.14 .venv
# shellcheck source=/dev/null
source .venv/bin/activate
PYTHON="${WORK}/.venv/bin/python"

# ---------------------------------------------------------------------------
# Build the engine from source.
#
# Not a wheel: the runners sys.path.insert the repo root, so an installed
# opteryx would be shadowed by the source tree anyway. Building is also what we
# want — the weekly measures main, not last month's release.
# ---------------------------------------------------------------------------
git clone --depth 50 https://github.com/mabel-dev/opteryx-core.git
git clone --depth 1 --branch "${HARNESS_REF}" https://github.com/mabel-dev/wrenchy-bench.git

cd "${WORK}/opteryx-core"
git checkout "${ENGINE_REF}"
uv pip install -r tests/requirements.txt 2>/dev/null || uv pip install cython setuptools setuptools-rust wheel
make compile

# ---------------------------------------------------------------------------
# Corpora. Synced read-only and verified against their manifests; never
# generated here. A missing corpus is a hard failure, not a forty-minute
# rebuild in the middle of a timed run.
# ---------------------------------------------------------------------------
sync_corpus() {
    local name="$1" dest="$2"
    echo "--- syncing ${name} -> ${dest}"
    mkdir -p "${dest}"
    aws s3 sync --only-show-errors "${CORPUS_PREFIX}/${name}/" "${dest}/"
}

# Six Skene mirrors and one parquet corpus — ClickBench-parquet is a suite line
# in its own right. JOB and H2O are Skene too: upstream ships rows, not files,
# so the format was always this harness's choice and is made once, everywhere.
#
# Synced from S3 rather than from the canonical GCS copy: GCS to EC2 is
# internet egress at ~$0.12/GB (~$9.60 a week, against ~$3 for the compute),
# where S3 in-region is free. Same bytes, same manifest.
sync_corpus tpch_1_skene   "${WORK}/opteryx-core/testdata/tpch_1_skene"
sync_corpus tpch_10_skene  "${WORK}/opteryx-core/testdata/tpch_10_skene"
sync_corpus tpch_100_skene "${WORK}/opteryx-core/testdata/tpch_100_skene"
sync_corpus job_skene      "${WORK}/opteryx-core/testdata/job_skene"
sync_corpus h2o_skene      "${WORK}/opteryx-core/testdata/h2o_skene"
sync_corpus hits_rugo_262k "${WORK}/opteryx-core/scratch/hits_rugo_262k"
sync_corpus hits_skene     "${WORK}/opteryx-core/scratch/hits_skene"

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
TOKEN=$(curl -sX PUT "http://169.254.169.254/latest/api/token" \
    -H "X-aws-ec2-metadata-token-ttl-seconds: 300")
imds() { curl -s -H "X-aws-ec2-metadata-token: ${TOKEN}" "http://169.254.169.254/latest/meta-data/$1"; }

export BENCH_INSTANCE_TYPE="$(imds instance-type)"
export BENCH_AZ="$(imds placement/availability-zone)"
export BENCH_STORAGE="${BENCH_STORAGE:-$(lsblk -ndo ROTA,SIZE / | tr -s ' ')}"

cd "${WORK}/opteryx-core"
"${PYTHON}" "${WORK}/wrenchy-bench/harness/run_suite.py" \
    --checkout "${WORK}/opteryx-core" \
    --out "${BUNDLE}" \
    --python "${PYTHON}" \
    --run-id "${RUN_ID}" || echo "suite exited non-zero; publishing what it produced"

# ---------------------------------------------------------------------------
# Publish. STATUS goes LAST and separately: it is what the Action polls and
# what the alarm watches, so it must not appear before the bundle beside it is
# complete.
# ---------------------------------------------------------------------------
cp "${LOG}" "${BUNDLE}/bootstrap.log" || true
aws s3 sync --only-show-errors --exclude STATUS "${BUNDLE}/" "${RESULTS_BUCKET}/runs/${RUN_ID}/"
aws s3 cp "${BUNDLE}/STATUS" "${RESULTS_BUCKET}/runs/${RUN_ID}/STATUS"

echo "=== complete: ${RESULTS_BUCKET}/runs/${RUN_ID}/"
trap - ERR
shutdown -h now
