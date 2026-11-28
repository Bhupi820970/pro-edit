#!/usr/bin/env bash
# Self-heal for the box after a reset / replacement (User, Nov 28: "kaam ruke na").
# Idempotent and quiet. Safe to run any time (shell start hook, hourly cron, scheduled bot check).
#  1. cron installed + running        2. all pro-edit cron lines present
#  3. box code copy synced from pro-edit main (code only, never local.env / data)
#  4. clip fetcher started if it has not finished a cycle in 4 h and is not running
# Prints one line per fix; exit 1 only if something could not be fixed.
set -u
BOX=/workspace/gta6/vicestyle/box
PE=/workspace/pe_repo
DATA=/workspace/gta6/box_fetch
LOG=$DATA/self_heal.log
mkdir -p "$DATA"
fail=0
say(){ echo "$(date '+%F %T %Z') $*" | tee -a "$LOG"; }

# 1. cron
if ! command -v crontab >/dev/null 2>&1; then
  if (sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq cron) >/dev/null 2>&1; then
    say "fixed: cron installed"
  else say "FAIL: could not install cron"; fail=1; fi
fi
if command -v crontab >/dev/null 2>&1 && ! pgrep -x cron >/dev/null 2>&1; then
  sudo service cron start >/dev/null 2>&1 || sudo cron >/dev/null 2>&1
  pgrep -x cron >/dev/null 2>&1 && say "fixed: cron daemon started" || { say "FAIL: cron daemon not running"; fail=1; }
fi

# 1b. python deps + ffmpeg (a fresh box may lose them)
if ! python3 -c "import yt_dlp, numpy, librosa, soundfile, PIL" >/dev/null 2>&1; then
  python3 -m pip install -q --user --break-system-packages -r "$PE/box/requirements-box.txt" >/dev/null 2>&1 \
    && say "fixed: python deps installed" || { say "FAIL: python deps"; fail=1; }
fi
command -v ffmpeg >/dev/null 2>&1 || { sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ffmpeg >/dev/null 2>&1 \
  && say "fixed: ffmpeg installed" || { say "FAIL: ffmpeg"; fail=1; }; }

# 3. sync code (before cron lines, so scripts are current)
if [ -d "$PE/.git" ] && [ -d "$BOX" ]; then
  if git -C "$PE" pull -q --ff-only origin main >/dev/null 2>&1 || true; then
    before=$(cd "$BOX/.." && cat box_fetch.py pipeline_lib/*.py config/*.json 2>/dev/null | md5sum)
    for f in "$PE"/*.py; do cp -p "$f" "$BOX/../" ; done
    for d in pipeline_lib config; do [ -d "$PE/$d" ] && cp -rp "$PE/$d/." "$BOX/../$d/"; done
    for f in "$PE"/box/*.sh "$PE"/box/*.py; do
      [ -e "$f" ] || continue; b=$(basename "$f"); [ "$b" = local.env ] && continue
      cp -p "$f" "$BOX/$b"; chmod +x "$BOX/$b" 2>/dev/null
    done
    after=$(cd "$BOX/.." && cat box_fetch.py pipeline_lib/*.py config/*.json 2>/dev/null | md5sum)
    [ "$before" != "$after" ] && say "fixed: box code synced from pro-edit $(git -C "$PE" rev-parse --short HEAD)"
  fi
fi

# 2. cron lines (pro-edit set + chrome check + this script hourly)
if command -v crontab >/dev/null 2>&1; then
  out=$(bash "$BOX/ensure_cron.sh" install 2>&1); [ -n "$out" ] && say "fixed: $out"
  for l in "0 */6 * * * $BOX/ensure_chrome_yt.sh" "41 * * * * $BOX/self_heal.sh" "5,35 * * * * $BOX/sky_guardian_kick.sh"; do
    crontab -l 2>/dev/null | grep -qxF "$l" || { { crontab -l 2>/dev/null; echo "$l"; } | crontab -; say "fixed: added $l"; }
  done
  bash "$BOX/ensure_cron.sh" hook >/dev/null 2>&1
fi

# 4. fetcher freshness
hb=$DATA/box_heartbeat.json
age=999999; [ -f "$hb" ] && age=$(( $(date +%s) - $(stat -c %Y "$hb") ))
if [ "$age" -gt 14400 ] && ! pgrep -f "box_fetch.py cycle" >/dev/null 2>&1; then
  nohup "$BOX/fetch_sources.sh" >/dev/null 2>&1 &
  say "fixed: clip fetcher was idle $((age/3600)) h -> started"
fi
exit $fail
