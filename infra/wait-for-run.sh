#!/usr/bin/env bash
#
# Block until the run bundle's STATUS object appears, or give up.
#
# STATUS is written last and separately by the bootstrap, so its presence means
# the bundle beside it is complete. Polling for it is the whole synchronisation
# protocol between the Action and the box — no SSM channel, no callback, no
# GitHub credential on the instance.

set -Eeuo pipefail

: "${RUN_ID:?}" "${RESULTS_BUCKET:?}"

DEADLINE_MINUTES="${DEADLINE_MINUTES:-450}" # instance watchdog is 420
POLL_SECONDS="${POLL_SECONDS:-60}"

TARGET="${RESULTS_BUCKET}/runs/${RUN_ID}/STATUS"
deadline=$((SECONDS + DEADLINE_MINUTES * 60))

echo "waiting for ${TARGET} (up to ${DEADLINE_MINUTES}m)"

while ((SECONDS < deadline)); do
    if aws s3 ls "${TARGET}" >/dev/null 2>&1; then
        STATUS=$(aws s3 cp "${TARGET}" - 2>/dev/null | tr -d '[:space:]')
        echo "run finished after $((SECONDS / 60))m with status: ${STATUS}"
        # `failed` is still collected: a partial bundle carries the lines that
        # did complete, and the console logs are the only record of why the
        # rest did not.
        exit 0
    fi

    # Report progress from the objects already uploaded rather than just
    # sleeping, so a stall is distinguishable from a slow line.
    COUNT=$(aws s3 ls --recursive "${RESULTS_BUCKET}/runs/${RUN_ID}/" 2>/dev/null | wc -l | tr -d ' ')
    echo "  $((SECONDS / 60))m elapsed · ${COUNT} objects uploaded so far"
    sleep "${POLL_SECONDS}"
done

echo "::error::no STATUS after ${DEADLINE_MINUTES}m — the instance died before it could report"
exit 1
