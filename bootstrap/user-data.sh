#!/usr/bin/env bash
#
# Instance bootstrap for the weekly benchmark run. Passed as EC2 user-data by
# the GitHub Actions launcher; templated with the values below.
#
#   ENGINE_VERSION  opteryx-core version to install and measure ("latest" or a pin)
#   HARNESS_REF     git ref of this repo
#   CORPUS_PREFIX   s3://opteryx-bench-corpora/v2026-08
#   RESULTS_BUCKET  s3://…
#   RUN_ID          the run identifier the Action is waiting on
#
# The instance does exactly one thing: produce a run bundle and put it in S3.
# It holds no GitHub credentials and no Opteryx credentials — comparison,
# publishing and the site are all downstream in Actions, where they can be
# re-run without paying for another four hours of EC2.

set -Eeuo pipefail

ENGINE_VERSION="${ENGINE_VERSION:-latest}"
HARNESS_REF="${HARNESS_REF:-main}"
CORPUS_PREFIX="${CORPUS_PREFIX:?}"
RESULTS_BUCKET="${RESULTS_BUCKET:?}"
RUN_ID="${RUN_ID:?}"

WORK=/opt/bench
DATA="${WORK}/data"
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

# ---------------------------------------------------------------------------
# Every external fetch below goes through this rather than running bare.
#
# What actually killed the first attempt at this run: `curl -fsSL
# https://astral.sh/uv/install.sh | sh` hung for 8+ minutes at ~0% CPU and
# near-zero network. Traced it — the installer script fetches ITSELF fine,
# prints its "downloading uv ..." progress line, and only THEN calls the real
# download: a bare `curl -sSfL "$1" -o "$2"` with NO timeout flags of any
# kind, buried inside a third-party script we do not control. A stalled
# TCP/TLS handshake there — not a slow transfer, a connection that never
# completes — blocks forever: invisible to `set -e` (nothing has failed),
# invisible to the ERR trap (nothing has errored), and invisible to the log
# until the 7h watchdog eventually reaps the box having done nothing for most
# of the night.
#
# `timeout --signal=KILL` bounds the WALL TIME of the whole command, so it
# does not matter whether the hang is inside our own curl or inside someone
# else's — three attempts with a short backoff, then a hard failure that DOES
# trip the ERR trap normally, because by the time this is used for anything
# but the aws cli itself, aws is already installed and can report it.
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


# ---------------------------------------------------------------------------
# Toolchain
# ---------------------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git curl unzip

# ⛔ THE AWS CLI IS NOT IN THE UBUNTU BASE IMAGE. Canonical ships no awscli;
# only Amazon Linux does. This script calls `aws` five times — corpus sync,
# bundle upload, STATUS, and the error handler — so without this the run gets
# all the way through `make compile`, dies at the first sync, and CANNOT SAY SO:
# the ERR trap's own reporting is an `aws s3 cp`. The result is no STATUS, no
# bundle, no log, and a box idling until the 7h watchdog. Installed FIRST, and
# asserted, so a failure here is loud and immediate rather than silent and
# twenty minutes later.
retry_with_timeout "aws cli download" 60 \
    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o /tmp/awscliv2.zip
unzip -q /tmp/awscliv2.zip -d /tmp
/tmp/aws/install
aws --version || { echo "FATAL: aws cli did not install; nothing downstream can report"; exit 1; }

# Ship the log to S3 every 30s for the whole run.
#
# Without this a run is INVISIBLE until it finishes: the bundle uploads at the
# end, there is no SSH (egress-only SG, no key pair), and EC2 console output
# lags by minutes and truncates. A four-hour first run with no window into it
# is how a fifteen-minute bug costs seven hours.
#
# Started after the CLI install for the obvious reason, and backgrounded so it
# cannot block the run. `|| true` because a transient S3 error must never take
# down the benchmark it is only observing.
(
    while true; do
        aws s3 cp --only-show-errors "${LOG}" \
            "${RESULTS_BUCKET}/runs/${RUN_ID}/progress.log" 2>/dev/null || true
        sleep 30
    done
) &
PROGRESS_PID=$!
trap 'kill ${PROGRESS_PID} 2>/dev/null || true' EXIT


retry_with_timeout "uv installer" 60 bash -c 'curl -fsSL https://astral.sh/uv/install.sh | sh'
export PATH="${HOME}/.local/bin:${PATH}"

# Stock CPython 3.14, NOT the free-threaded 3.14t build. opteryx-core stopped
# publishing cp314t wheels after 0.9.16 and the parallelism target is native
# threads under a released GIL — 3.14t here would silently change what is
# being measured.
retry_with_timeout "python 3.14 install" 180 uv python install 3.14
cd "${WORK}"
uv venv --python 3.14 .venv
# shellcheck source=/dev/null
source .venv/bin/activate
PYTHON="${WORK}/.venv/bin/python"

# ---------------------------------------------------------------------------
# Install the engine.
#
# The PUBLISHED WHEEL, not a source build. opteryx-core releases four or five
# times a week, so a wheel tracks development at better than weekly resolution
# — which is all a weekly benchmark can resolve anyway — and it measures what
# users actually get. It also takes the entire toolchain off this box: no rust,
# no clang, no twenty-minute `make compile`, and no build that can fail at
# 02:00 on a Sunday.
#
# The version is recorded in the run manifest, so a number is always
# attributable to an exact release rather than to "whatever main was".
# ---------------------------------------------------------------------------
retry_with_timeout "harness clone" 60 bash -c \
    "rm -rf '${WORK}/wrenchy-bench' && git clone --depth 1 --branch '${HARNESS_REF}' \
    https://github.com/mabel-dev/wrenchy-bench.git '${WORK}/wrenchy-bench'"

if [[ "${ENGINE_VERSION}" == "latest" ]]; then
    PIP_SPEC="opteryx-core"
else
    PIP_SPEC="opteryx-core==${ENGINE_VERSION}"
fi
retry_with_timeout "opteryx-core install" 180 uv pip install --python "${PYTHON}" "${PIP_SPEC}"

# Datasets resolve relative to the WORKING DIRECTORY, not to the package, so
# the corpora need a root to sit under — there is no checkout any more.
mkdir -p "${DATA}/testdata" "${DATA}/scratch"

"${PYTHON}" -c "import opteryx; print(f'engine {opteryx.__version__}+{opteryx.__build__} from {opteryx.__file__}')"

# ---------------------------------------------------------------------------
# Corpora. Synced read-only and verified against their manifests; never
# generated here. A missing corpus is a hard failure, not a forty-minute
# rebuild in the middle of a timed run.
# ---------------------------------------------------------------------------
sync_corpus() {
    local name="$1" dest="$2"
    echo "--- syncing ${name} -> ${dest}"
    mkdir -p "${dest}"
    # Not wrapped in retry_with_timeout: the AWS CLI applies its own 60s
    # connect/read timeouts and retry logic by default, unlike the bare curl
    # that hung above. If this line stalls indefinitely too, that default has
    # changed and is worth a second look rather than a bigger hammer here.
    aws s3 sync --only-show-errors "${CORPUS_PREFIX}/${name}/" "${dest}/"
}

# Six Skene mirrors and one parquet corpus — ClickBench-parquet is a suite line
# in its own right. JOB and H2O are Skene too: upstream ships rows, not files,
# so the format was always this harness's choice and is made once, everywhere.
#
# Read from S3 in this region, where the transfer is free. The same bytes also
# exist in GCS so the engine can query them as datasets (Opteryx has a GCS
# filesystem and no S3 one), but the runner never reads that copy: GCS egress
# to EC2 would be ~$9 a run, more than the compute it feeds.
sync_corpus tpch_1_skene   "${DATA}/testdata/tpch_1_skene"
sync_corpus tpch_10_skene  "${DATA}/testdata/tpch_10_skene"
sync_corpus tpch_100_skene "${DATA}/testdata/tpch_100_skene"
sync_corpus job_skene      "${DATA}/testdata/job_skene"
sync_corpus h2o_skene      "${DATA}/testdata/h2o_skene"
sync_corpus hits_partitioned "${DATA}/scratch/hits_partitioned"
sync_corpus hits_skene     "${DATA}/scratch/hits_skene"

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
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
