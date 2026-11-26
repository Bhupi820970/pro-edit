#!/usr/bin/env bash
# Vice Cartel box downloader — plain script, no AI. Cron, every 3 hours (box clock = IST):
#   50 1-23/3 * * * <repo>/box/fetch_sources.sh
# (01:50 04:50 ... 22:50 IST, 40 min before each produce slot.)
# box_fetch.py cycle --no-gemini: reads the trusted fetch queue (JSON data on branch pipeline-state), tops up
# the genre clip pool from official channels (>=1080p, logo/caption checks), uploads clips + manifest.json to
# the DRAFT release "sources", refills the "songs" release, then frees disk. Never commits videos or browser
# data. Never runs code from the queue, issues or AI output.
set -u
. "$(dirname "$0")/common.sh"
LOG="$RUN/fetch_sources.log"
rotate_log "$LOG" 20000000
if ! take_lock "$RUN/fetch_sources.lock"; then
  echo "$(date '+%F %T %Z') previous fetch still running -> skip" >> "$LOG"; exit 0
fi
echo "$(date '+%F %T %Z') fetch_sources start ($(cd "$REPO_DIR" && git rev-parse --short HEAD 2>/dev/null))" >> "$LOG"
cd "$REPO_DIR" || exit 1
tmo 170m lowprio python3 "$REPO_DIR/box_fetch.py" cycle --no-gemini --interval 10800 --dir "$RUN" >> "$LOG" 2>&1
rc=$?
echo "$(date '+%F %T %Z') fetch_sources end rc=$rc" >> "$LOG"
exit $rc
