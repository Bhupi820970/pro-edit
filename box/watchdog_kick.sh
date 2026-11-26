#!/usr/bin/env bash
# Backup trigger for the watchdog workflow (GitHub drops scheduled runs). Plain script, no AI: it only asks
# GitHub to run watchdog.yml (auto-produce when stock < 3, health issue). It also restarts the clip
# fetcher when it died (no finished cycle for 4 h) or when the clip pool is low (<= 50), with a backoff
# of 30/60/120/180 min while cycles add nothing, so the pool refills without a manual step.
#   cron (IST):  28,58 * * * *
set -u
. "$(dirname "$0")/common.sh"
LOG="$RUN/watchdog_kick.log"
rotate_log "$LOG" 1000000
# clip fetcher: box_fetch.py should-run only reads pool_state.json; fetch_sources.sh holds the lock, so a
# fetch that is still running is never doubled
if [ "$(cd "$REPO_DIR" && tmo 60 python3 "$REPO_DIR/box_fetch.py" should-run --dir "$RUN" 2>/dev/null)" = "yes" ]; then
  nohup bash "$REPO_DIR/box/fetch_sources.sh" >/dev/null 2>&1 < /dev/null &
  echo "$(date '+%F %T %Z') clip fetcher (re)started (pool low / fetcher idle too long)" >> "$LOG"
fi
for i in 1 2 3; do
  if tmo 60 gh workflow run watchdog.yml --repo "$VICE_REPO" --ref main >> "$LOG" 2>&1; then
    echo "$(date '+%F %T %Z') watchdog.yml dispatched" >> "$LOG"; exit 0
  fi
  sleep $((i * 20))
done
echo "$(date '+%F %T %Z') watchdog.yml dispatch FAILED" >> "$LOG"; exit 1
