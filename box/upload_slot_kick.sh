#!/usr/bin/env bash
# Backup trigger for the IST upload slots (plain script, no AI, runs nothing from the repo/queue/issues).
# GitHub regularly drops or delays scheduled workflow runs (Nov 26: no upload.yml schedule run at all
# between 09:12 and 16:46 IST although it has crons every 20 min), so the box asks GitHub to run
# upload.yml in mode=slot a few minutes after each slot. upload.py posts only if the current IST slot
# is due and not yet in ledger/posted.json, so extra kicks never double-post.
#   crontab (box clock = IST):  3 8,15 * * *   and   33 11,18,21 * * *
set -u
RUN=/workspace/gta6/box_fetch
LOG="$RUN/upload_slot_kick.log"
mkdir -p "$RUN"
export PATH="/usr/local/bin:/usr/bin:/bin"
if [ -f "$RUN/box.env" ]; then
  while IFS='=' read -r k v; do
    if [ "$k" = "GH_CONFIG_DIR" ] && [[ "$v" =~ ^[A-Za-z0-9:/._+-]{1,200}$ ]]; then export GH_CONFIG_DIR="$v"; fi
  done < "$RUN/box.env"
fi
if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG")" -gt 2000000 ]; then mv -f "$LOG" "$LOG.1"; fi
for i in 1 2 3; do
  if timeout 60 gh workflow run upload.yml --repo Bhupi820970/pro-edit --ref main -f mode=slot >> "$LOG" 2>&1; then
    echo "$(date '+%F %T %Z') upload.yml mode=slot dispatched" >> "$LOG"
    exit 0
  fi
  sleep $((i * 30))
done
echo "$(date '+%F %T %Z') upload.yml dispatch FAILED" >> "$LOG"
exit 1
