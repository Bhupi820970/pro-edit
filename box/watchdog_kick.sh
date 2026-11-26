#!/usr/bin/env bash
# Backup trigger for the watchdog workflow (GitHub drops scheduled runs). Plain script, no AI: it only asks
# GitHub to run watchdog.yml (auto-produce when stock < 3, health issue).
#   cron (IST):  28,58 * * * *
set -u
. "$(dirname "$0")/common.sh"
LOG="$RUN/watchdog_kick.log"
rotate_log "$LOG" 1000000
for i in 1 2 3; do
  if tmo 60 gh workflow run watchdog.yml --repo "$VICE_REPO" --ref main >> "$LOG" 2>&1; then
    echo "$(date '+%F %T %Z') watchdog.yml dispatched" >> "$LOG"; exit 0
  fi
  sleep $((i * 20))
done
echo "$(date '+%F %T %Z') watchdog.yml dispatch FAILED" >> "$LOG"; exit 1
