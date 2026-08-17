#!/usr/bin/env bash
#
# Instance bootstrap for the weekly benchmark run. Passed as EC2 user-data by
# the launcher; templated with the values below.
#
#   ENGINE_VERSION  opteryx-core version to install and measure ("latest" or a pin)
#   HARNESS_REF     git ref of this repo
#   CORPUS_PREFIX   s3://opteryx-bench-corpora/v2026-08
#   RESULTS_BUCKET  s3://…
#   RUN_ID          the run identifier the launcher is waiting on
#
# The instance does exactly one thing: produce a run bundle and put it in S3.
# It holds no GitHub or Opteryx credentials — comparison, publishing and the
# site are all downstream, where they can re-run without paying for EC2 again.
#
# ⛔ EC2 user-data has a real ceiling well under the documented 16KB/25600B
# figures once launched with a subnet+profile (empirically ~16000B encoded,
# found 2026-08-17 by bisection against production — see git log). Every
# comment in this file is short ON PURPOSE. Put the "why" in commit messages,
# not here.

set -euo pipefail
# No -E: an ERR trap did not fire for a set -u nounset abort (2026-08-16);
# see on_exit below, which does not rely on that guarantee.

ENGINE_VERSION="${ENGINE_VERSION:-latest}"
HARNESS_REF="${HARNESS_REF:-main}"
CORPUS_PREFIX="${CORPUS_PREFIX:?}"
RESULTS_BUCKET="${RESULTS_BUCKET:?}"
RUN_ID="${RUN_ID:?}"

# HOME is unset under cloud-init (not a login shell); fatal under set -u
# wherever referenced (uv/git/pip all consult it too, not just PATH).
export HOME=/root

WORK=/opt/bench
DATA="${WORK}/data"
BUNDLE="${WORK}/bundle"
LOG=/var/log/bench-bootstrap.log

exec > >(tee -a "${LOG}") 2>&1

# Watchdog first, before anything that could hang. 7h = run budget + headroom.
shutdown -h +420 "benchmark watchdog" &

# ONE handler, on EXIT not ERR: ERR does not reliably fire for a `set -u`
# nounset abort (confirmed by test: a bare nounset trap prints $?=0, not
# nonzero), so this does not key off the exit code at all. It defaults to
# FAILED and only COMPLETED=1 (set at the true end, below) clears that.
COMPLETED=0

on_exit() {
    trap - EXIT
    if [[ -n "${PROGRESS_PID:-}" ]]; then
        kill "${PROGRESS_PID}" 2>/dev/null || true
    fi
    # Unconditional — the only debugging surface a dead run has.
    aws s3 cp "${LOG}" "${RESULTS_BUCKET}/runs/${RUN_ID}/bootstrap.log" 2>/dev/null || true
    if [[ "${COMPLETED}" != "1" ]]; then
        echo "FAILED — did not reach the end of the script"
        echo "failed" > /tmp/STATUS
        aws s3 cp /tmp/STATUS "${RESULTS_BUCKET}/runs/${RUN_ID}/STATUS" 2>/dev/null || true
    fi
    shutdown -h now
}
trap on_exit EXIT

# Bounds the WALL TIME of any command, ours or a third party's — a bare curl
# inside uv's own installer once hung 8+ min with no timeout of its own and no
# visible failure. Three attempts, short backoff, then a hard failure that
# reaches on_exit normally (aws is installed by the time this is used for
# anything but its own download).
retry_with_timeout() {
    local desc="$1" secs="$2"
    shift 2
    local attempt
    for attempt in 1 2 3; do
        echo "--- ${desc} (attempt ${attempt}/3, ${secs}s bound)"
        if timeout --signal=KILL "${secs}" "$@"; then
            return 0
        fi
        echo "    ${desc} attempt ${attempt} failed or timed out"
        [[ ${attempt} -lt 3 ]] && sleep $((attempt * 5))
    done
    echo "FATAL: ${desc} failed after 3 attempts"
    return 1
}

echo "=== weekly bench ${RUN_ID} · engine ${ENGINE_VERSION} · harness ${HARNESS_REF}"
mkdir -p "${WORK}"

# --- Toolchain ---------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git curl unzip

# Canonical's image ships no awscli. Everything downstream reports via
# `aws s3 cp`, so this goes first and is asserted.
retry_with_timeout "aws cli download" 60 \
    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o /tmp/awscliv2.zip
unzip -q /tmp/awscliv2.zip -d /tmp
/tmp/aws/install
aws --version || { echo "FATAL: aws cli did not install; nothing downstream can report"; exit 1; }

# Ship the log to S3 every 30s. No SSH exists on this box (egress-only SG, no
# key pair) and EC2 console output lags and truncates — this is the only live
# window into a multi-hour run.
(
    while true; do
        aws s3 cp --only-show-errors "${LOG}" \
            "${RESULTS_BUCKET}/runs/${RUN_ID}/progress.log" 2>/dev/null || true
        sleep 30
    done
) &
PROGRESS_PID=$!

retry_with_timeout "uv installer" 60 bash -c 'curl -fsSL https://astral.sh/uv/install.sh | sh'
export PATH="${HOME}/.local/bin:${PATH}"

# Stock CPython 3.14, not 3.14t: 3.14t wheels stopped after 0.9.16 and the
# parallelism target is native threads under a released GIL, not free-thread.
retry_with_timeout "python 3.14 install" 180 uv python install 3.14
cd "${WORK}"
uv venv --python 3.14 .venv
# shellcheck source=/dev/null
source .venv/bin/activate
PYTHON="${WORK}/.venv/bin/python"

# --- Install the engine -------------------------------------------------
# The PUBLISHED WHEEL, not a source build: opteryx-core releases 4-5x/week,
# so a wheel resolves finer than a weekly benchmark can anyway, measures what
# users get, and removes the whole build toolchain from this box. Version is
# recorded in the run manifest.
retry_with_timeout "harness clone" 60 bash -c \
    "rm -rf '${WORK}/wrenchy-bench' && git clone --depth 1 --branch '${HARNESS_REF}' \
    https://github.com/mabel-dev/wrenchy-bench.git '${WORK}/wrenchy-bench'"

if [[ "${ENGINE_VERSION}" == "latest" ]]; then
    PIP_SPEC="opteryx-core"
else
    PIP_SPEC="opteryx-core==${ENGINE_VERSION}"
fi
retry_with_timeout "opteryx-core install" 180 uv pip install --python "${PYTHON}" "${PIP_SPEC}"

# Datasets resolve relative to the WORKING DIRECTORY, not the package.
mkdir -p "${DATA}/testdata" "${DATA}/scratch"

"${PYTHON}" -c "import opteryx; print(f'engine {opteryx.__version__}+{opteryx.__build__} from {opteryx.__file__}')"

# --- Corpora -------------------------------------------------------------
# Never generated here — a missing corpus is a hard failure, not a rebuild.
sync_corpus() {
    local name="$1" dest="$2"
    echo "--- syncing ${name} -> ${dest}"
    mkdir -p "${dest}"
    # Not retry-wrapped: aws s3 has its own 60s connect/read timeouts.
    aws s3 sync --only-show-errors "${CORPUS_PREFIX}/${name}/" "${dest}/"
}

# All Skene except ClickBench-parquet, which IS the parquet line. JOB/H2O
# ship rows upstream, not files, so the format was always ours to pick.
sync_corpus tpch_1_skene   "${DATA}/testdata/tpch_1_skene"
sync_corpus tpch_10_skene  "${DATA}/testdata/tpch_10_skene"
sync_corpus tpch_100_skene "${DATA}/testdata/tpch_100_skene"
sync_corpus job_skene      "${DATA}/testdata/job_skene"
sync_corpus h2o_skene      "${DATA}/testdata/h2o_skene"
sync_corpus hits_partitioned "${DATA}/scratch/hits_partitioned"
sync_corpus hits_skene     "${DATA}/scratch/hits_skene"

# --- Run -------------------------------------------------------------------
TOKEN=$(curl -sX PUT "http://169.254.169.254/latest/api/token" \
    -H "X-aws-ec2-metadata-token-ttl-seconds: 300")
imds() { curl -s -H "X-aws-ec2-metadata-token: ${TOKEN}" "http://169.254.169.254/latest/meta-data/$1"; }

export BENCH_INSTANCE_TYPE="$(imds instance-type)"
export BENCH_AZ="$(imds placement/availability-zone)"
export BENCH_STORAGE="${BENCH_STORAGE:-$(lsblk -ndo ROTA,SIZE / | tr -s ' ')}"

cd "${DATA}"
"${PYTHON}" "${WORK}/wrenchy-bench/harness/run_suite.py" \
    --data-root "${DATA}" \
    --out "${BUNDLE}" \
    --python "${PYTHON}" \
    --run-id "${RUN_ID}" || echo "suite exited non-zero; publishing what it produced"

# STATUS goes last and separately — it is what gets polled and what the
# alarm watches, so it must not appear before the bundle beside it does.
cp "${LOG}" "${BUNDLE}/bootstrap.log" || true
aws s3 sync --only-show-errors --exclude STATUS "${BUNDLE}/" "${RESULTS_BUCKET}/runs/${RUN_ID}/"
aws s3 cp "${BUNDLE}/STATUS" "${RESULTS_BUCKET}/runs/${RUN_ID}/STATUS"

echo "=== complete: ${RESULTS_BUCKET}/runs/${RUN_ID}/"
COMPLETED=1
