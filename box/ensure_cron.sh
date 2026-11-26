#!/usr/bin/env bash
# Makes sure cron is installed + running and the Vice Cartel fetch line is in this user's crontab.
# Idempotent. Hooked into ~/.profile and ~/.bashrc so it comes back after a box reset.
#   bash /workspace/gta6/vicestyle/box/ensure_cron.sh            # install/ensure
#   bash /workspace/gta6/vicestyle/box/ensure_cron.sh status
set -u
LINE='50 1-23/3 * * * /workspace/gta6/vicestyle/box/fetch_sources.sh'
MARK="vice-cartel-fetch-sources"
HOOK_LINE='[ -x /workspace/gta6/vicestyle/box/ensure_cron.sh ] && /workspace/gta6/vicestyle/box/ensure_cron.sh >/dev/null 2>&1'

if [ "${1:-}" = "status" ]; then
  pgrep -x cron >/dev/null && echo "cron running" || echo "cron NOT running"
  crontab -l 2>/dev/null | grep -F "box/fetch_sources.sh" || echo "no fetch_sources line"
  exit 0
fi

if ! command -v crontab >/dev/null 2>&1; then
  { sudo -n apt-get update -qq && sudo -n apt-get install -y -qq cron; } >/dev/null 2>&1 || { echo "could not install cron"; exit 1; }
fi
if ! pgrep -x cron >/dev/null 2>&1; then
  sudo -n /usr/sbin/cron 2>/dev/null || sudo -n service cron start >/dev/null 2>&1 || true
fi
if ! crontab -l 2>/dev/null | grep -qF "box/fetch_sources.sh"; then
  { crontab -l 2>/dev/null; echo "$LINE"; } | crontab -   # box clock is IST (/etc/localtime)
  echo "crontab line added"
fi
for f in /home/box/.profile /home/box/.bashrc; do
  [ -f "$f" ] || continue
  grep -q "$MARK" "$f" 2>/dev/null && continue
  printf '\n# %s\n%s\n# %s-end\n' "$MARK" "$HOOK_LINE" "$MARK" >> "$f"
done
pgrep -x cron >/dev/null && echo "cron running" || echo "cron NOT running"
