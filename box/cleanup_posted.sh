#!/usr/bin/env bash
# Every-2-days disk cleanup of already-posted videos' clips/renders + temp leftovers (see cleanup_posted.py).
#   crontab (box clock = IST):  25 4 */2 * * /workspace/gta6/vicestyle/box/cleanup_posted.sh
# Plain script, data only; shares the fetcher's lock so it never runs while a download cycle is active.
set -u
DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN=/workspace/gta6/box_fetch
mkdir -p "$RUN"
LOG="$RUN/cleanup_posted.log"
export PATH="/home/box/.local/bin:/usr/local/bin:/usr/bin:/bin"
if [ -f "$RUN/box.env" ]; then
  while IFS='=' read -r k v; do
    if [ "$k" = "GH_CONFIG_DIR" ] && [[ "$v" =~ ^[A-Za-z0-9:/._+-]{1,200}$ ]]; then export GH_CONFIG_DIR="$v"; fi
  done < "$RUN/box.env"
fi
if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG")" -gt 2000000 ]; then mv -f "$LOG" "$LOG.1"; fi
exec 9>"$RUN/fetch_sources.lock"
if ! flock -n 9; then echo "$(date '+%F %T %Z') fetch running -> skip cleanup" >> "$LOG"; exit 0; fi
echo "$(date '+%F %T %Z') cleanup_posted start" >> "$LOG"
timeout 30m nice -n 10 ionice -c3 python3 "$DIR/box/cleanup_posted.py" "$@" >> "$LOG" 2>&1
rc=$?
echo "$(date '+%F %T %Z') cleanup_posted end rc=$rc" >> "$LOG"
exit $rc
