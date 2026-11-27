#!/usr/bin/env bash
# Every-2-days cleanup: records used clips in the no-repeat ledger, then frees disk of already-posted videos'
# clips/renders and temp leftovers (see cleanup_posted.py). Shares the fetcher's lock.
#   cron (IST):  25 4 */2 * *
set -u
. "$(dirname "$0")/common.sh"
LOG="$RUN/cleanup_posted.log"
rotate_log "$LOG" 2000000
if ! take_lock "$RUN/fetch_sources.lock"; then echo "$(date '+%F %T %Z') fetch running -> skip cleanup" >> "$LOG"; exit 0; fi
echo "$(date '+%F %T %Z') cleanup_posted start" >> "$LOG"
tmo 30m nice -n 10 python3 "$REPO_DIR/box/cleanup_posted.py" "$@" >> "$LOG" 2>&1
rc=$?
echo "$(date '+%F %T %Z') cleanup_posted end rc=$rc" >> "$LOG"
exit $rc
