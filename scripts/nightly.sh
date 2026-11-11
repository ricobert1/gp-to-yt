#!/bin/sh
# Nightly run: ingest -> upload --limit N -> curate -> report.
#
# Exit code: 0 if every step succeeded, 1 if any step failed.
# - ingest failing stops the run (upload and curate depend on it).
# - upload exits 1 when *some* videos failed; the others were uploaded, so
#   curate and report still run, and the final exit code is still 1.
# - A clean stop on quota is not a failure (exit 0), as in each command.
#
# UPLOAD_LIMIT (default 5) is the --limit for upload. With the default
# quota.daily_unit_limit 10000 and upload_cost_units 1600, 5 uploads use
# 8005 units and leave about 2000 for curate (playlist adds are 50 each).
# RECONCILE=1 also runs `reconcile` (1 unit per 50 uploaded videos) before report.
set -u

LIMIT="${UPLOAD_LIMIT:-5}"
status=0

run() {
    echo "== $(date -u +%Y-%m-%dT%H:%M:%SZ) photos2yt $*"
    photos2yt "$@"
    rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "== FAILED (exit $rc): photos2yt $*" >&2
        status=1
    fi
    return "$rc"
}

run ingest || { echo "== ingest failed; not uploading." >&2; exit 1; }
run upload --limit "$LIMIT"
run curate
if [ "${RECONCILE:-0}" = "1" ]; then
    run reconcile
fi
run report

echo "== $(date -u +%Y-%m-%dT%H:%M:%SZ) nightly done, exit $status"
exit "$status"
