#!/usr/bin/env bash
# Makes sure cron is installed + running and the Vice Cartel fetch line is in this user's crontab.
# Idempotent. Hooked into ~/.profile and ~/.bashrc so it comes back after a box reset.
#   bash /workspace/gta6/vicestyle/box/ensure_cron.sh            # install/ensure
#   bash /workspace/gta6/vicestyle/box/ensure_cron.sh status
set -u
LINE='50 1-23/3 * * * /workspace/gta6/vicestyle/box/fetch_sources.sh'
# backup kicks for the IST upload slots 08:00 11:30 15:00 18:30 21:30 (GitHub drops scheduled runs)
KICK1='3 8,15 * * * /workspace/gta6/vicestyle/box/upload_slot_kick.sh'
KICK2='33 11,18,21 * * * /workspace/gta6/vicestyle/box/upload_slot_kick.sh'
# every 2 days 04:25 IST: record used clips in the no-repeat ledger, then free disk of posted videos' clips/renders
CLEAN='25 4 */2 * * /workspace/gta6/vicestyle/box/cleanup_posted.sh'
# every 30 min: ask GitHub to run watchdog.yml (auto-produce when stock < 3, health issue)
WATCH='28,58 * * * * /workspace/gta6/vicestyle/box/watchdog_kick.sh'
MARK="vice-cartel-fetch-sources"
HOOK_LINE='[ -x /workspace/gta6/vicestyle/box/ensure_cron.sh ] && /workspace/gta6/vicestyle/box/ensure_cron.sh >/dev/null 2>&1'

if [ "${1:-}" = "status" ]; then
  pgrep -x cron >/dev/null && echo "cron running" || echo "cron NOT running"
  crontab -l 2>/dev/null | grep -F "box/fetch_sources.sh" || echo "no fetch_sources line"
  crontab -l 2>/dev/null | grep -F "box/upload_slot_kick.sh" || echo "no upload_slot_kick lines"
  crontab -l 2>/dev/null | grep -F "box/cleanup_posted.sh" || echo "no cleanup_posted line"
  crontab -l 2>/dev/null | grep -F "box/watchdog_kick.sh" || echo "no watchdog_kick line"
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
if ! crontab -l 2>/dev/null | grep -qF "box/upload_slot_kick.sh"; then
  { crontab -l 2>/dev/null; echo "$KICK1"; echo "$KICK2"; } | crontab -
  echo "upload slot kick lines added"
fi
if ! crontab -l 2>/dev/null | grep -qF "box/cleanup_posted.sh"; then
  { crontab -l 2>/dev/null; echo "$CLEAN"; } | crontab -
  echo "cleanup line added"
fi
if ! crontab -l 2>/dev/null | grep -qF "box/watchdog_kick.sh"; then
  { crontab -l 2>/dev/null; echo "$WATCH"; } | crontab -
  echo "watchdog kick line added"
fi
for f in /home/box/.profile /home/box/.bashrc; do
  [ -f "$f" ] || continue
  grep -q "$MARK" "$f" 2>/dev/null && continue
  printf '\n# %s\n%s\n# %s-end\n' "$MARK" "$HOOK_LINE" "$MARK" >> "$f"
done
pgrep -x cron >/dev/null && echo "cron running" || echo "cron NOT running"
