#!/usr/bin/env bash
# Backup trigger for the watchdog workflow (GitHub drops scheduled runs). Plain script, no AI: it only
# asks GitHub to run watchdog.yml; it reads nothing from the repo, issues or queue.
#   crontab (box clock = IST):  28,58 * * * *
set -u
RUN=/workspace/gta6/box_fetch
LOG="$RUN/watchdog_kick.log"
mkdir -p "$RUN"
export PATH="/usr/local/bin:/usr/bin:/bin"
if [ -f "$RUN/box.env" ]; then
  while IFS='=' read -r k v; do
    if [ "$k" = "GH_CONFIG_DIR" ] && [[ "$v" =~ ^[A-Za-z0-9:/._+-]{1,200}$ ]]; then export GH_CONFIG_DIR="$v"; fi
  done < "$RUN/box.env"
fi
if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG")" -gt 1000000 ]; then mv -f "$LOG" "$LOG.1"; fi
for i in 1 2 3; do
  if timeout 60 gh workflow run watchdog.yml --repo Bhupi820970/pro-edit --ref main >> "$LOG" 2>&1; then
    echo "$(date '+%F %T %Z') watchdog.yml dispatched" >> "$LOG"; exit 0
  fi
  sleep $((i * 20))
done
echo "$(date '+%F %T %Z') watchdog.yml dispatch FAILED" >> "$LOG"; exit 1
