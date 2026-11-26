#!/usr/bin/env bash
# Backup trigger for the IST upload slots (plain script, no AI). GitHub sometimes drops scheduled runs, so
# this asks GitHub to run upload.yml mode=slot a few minutes after each slot. upload.py posts only when the
# current slot is due and not yet in ledger/posted.json, so extra kicks never double-post.
#   cron (IST):  3 8,15 * * *   and   33 11,18,21 * * *
set -u
. "$(dirname "$0")/common.sh"
LOG="$RUN/upload_slot_kick.log"
rotate_log "$LOG" 2000000
for i in 1 2 3; do
  if tmo 60 gh workflow run upload.yml --repo "$VICE_REPO" --ref main -f mode=slot >> "$LOG" 2>&1; then
    echo "$(date '+%F %T %Z') upload.yml mode=slot dispatched" >> "$LOG"; exit 0
  fi
  sleep $((i * 30))
done
echo "$(date '+%F %T %Z') upload.yml dispatch FAILED" >> "$LOG"
exit 1
