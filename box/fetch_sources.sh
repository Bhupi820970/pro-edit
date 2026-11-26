#!/usr/bin/env bash
# Vice Cartel box downloader — plain script, no AI. Run by crontab every 3 hours (see box/ensure_cron.sh):
#   50 1-23/3 * * * /workspace/gta6/vicestyle/box/fetch_sources.sh
# (box clock is IST: 01:50 04:50 07:50 10:50 13:50 16:50 19:50 22:50 — 40 min before each produce.yml
#  slot at 05:30/11:30/17:30/23:30 IST, and off the Skywalker service's own schedule.)
#
# What it does (box_fetch.py cycle --no-gemini):
#   - reads the produce runs' fetch_queue.json on branch pipeline-state (plain JSON data, trust-checked;
#     Gemini-written search queries arrive here when the runner had quota)
#   - tops up each category's pool from the produce source pools in config/pipeline.json
#     (official channels + seed searches), counting only clips Gemini has not rejected
#   - downloads with yt-dlp from the box IP (argument lists only, YouTube URLs only)
#   - uploads clips + manifest.json to the DRAFT (private) release "sources" for CI, then frees disk
# Never commits videos or browser data to git. Never runs code from the repo queue, issues or AI output.
set -u
DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN=/workspace/gta6/box_fetch
mkdir -p "$RUN"
LOG="$RUN/fetch_sources.log"
LOCK="$RUN/fetch_sources.lock"
export PATH="/home/box/.local/bin:/home/box/.deno/bin:/usr/local/bin:/usr/bin:/bin"
export PYTHONUNBUFFERED=1

# Box-only settings live OUTSIDE git in $RUN/box.env (KEY=value lines). Only these keys are read and
# values must be plain paths/names — the file is parsed, never sourced/executed.
if [ -f "$RUN/box.env" ]; then
  while IFS='=' read -r k v; do
    case "$k" in
      GH_CONFIG_DIR|VICE_YT_COOKIES_BROWSER|VICE_YTDLP_JS_RUNTIME|BOX_FETCH_MAX_PER_CAT|BOX_FETCH_MIN_FREE_GB|BOX_FETCH_SIBLINGS)
        if [[ "$v" =~ ^[A-Za-z0-9:/._+-]{1,200}$ ]]; then export "$k=$v"; fi ;;
    esac
  done < "$RUN/box.env"
fi

# keep the log small
if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG")" -gt 20000000 ]; then mv -f "$LOG" "$LOG.1"; fi

# one run at a time; if the previous run is still going, skip this slot
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date '+%F %T %Z') previous fetch still running -> skip" >> "$LOG"
  exit 0
fi

echo "$(date '+%F %T %Z') fetch_sources start ($(cd "$DIR" && git rev-parse --short HEAD 2>/dev/null))" >> "$LOG"
cd "$DIR" || exit 1
# low priority so the Skywalker downloader and the desktop stay responsive; hard 170 min cap
timeout 170m nice -n 10 ionice -c3 python3 "$DIR/box_fetch.py" cycle --no-gemini --interval 10800 \
  --dir "$RUN" >> "$LOG" 2>&1
rc=$?
echo "$(date '+%F %T %Z') fetch_sources end rc=$rc" >> "$LOG"
exit $rc
