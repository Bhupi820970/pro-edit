#!/usr/bin/env bash
# Installs / shows / removes the Vice Cartel (pro-edit) cron lines for the current user. Idempotent.
# Cron times are for a computer whose clock is set to IST (Asia/Kolkata). On another time zone, convert them
# (or run with TZ=Asia/Kolkata where cron supports it).
#   bash box/ensure_cron.sh install     # add any missing line (default when no argument is given)
#   bash box/ensure_cron.sh status      # show the lines
#   bash box/ensure_cron.sh remove      # delete every pro-edit line + the optional shell-start hook
#   bash box/ensure_cron.sh hook        # optional: re-run "install" from ~/.profile/~/.bashrc (box resets)
# Only ONE computer may run these lines, otherwise the downloader and the kicks run twice.
set -u
B="$(cd "$(dirname "$0")" && pwd)"
LINES=(
  "50 1-23/3 * * * $B/fetch_sources.sh"        # clip + song downloader, every 3 h
  "3 8,15 * * * $B/upload_slot_kick.sh"         # backup kick for the 08:00 / 15:00 upload slots
  "33 11,18,21 * * * $B/upload_slot_kick.sh"    # backup kick for the 11:30 / 18:30 / 21:30 slots
  "25 4 * * * $B/cleanup_posted.sh"             # used-clip ledger + disk cleanup, daily
  "28,58 * * * * $B/watchdog_kick.sh"           # backup kick for watchdog.yml (auto-produce, health)
  "17 3 * * 0 $B/ig_token_refresh.sh"           # weekly IG token refresh (Instagram-Login tokens only)
)
MARK="vice-cartel-fetch-sources"
HOOK_LINE="[ -x $B/ensure_cron.sh ] && $B/ensure_cron.sh install >/dev/null 2>&1"
cmd="${1:-install}"
case "$cmd" in
  status)
    for l in "${LINES[@]}"; do crontab -l 2>/dev/null | grep -qxF "$l" && echo "on : $l" || echo "off: $l"; done
    ;;
  install)
    command -v crontab >/dev/null 2>&1 || { echo "cron is not installed (Linux: sudo apt-get install cron)"; exit 1; }
    # drop the old every-2-days cleanup line (now daily)
    crontab -l 2>/dev/null | grep -qxF "25 4 */2 * * $B/cleanup_posted.sh" && crontab -l 2>/dev/null | grep -vxF "25 4 */2 * * $B/cleanup_posted.sh" | crontab -
    for l in "${LINES[@]}"; do
      crontab -l 2>/dev/null | grep -qxF "$l" || { { crontab -l 2>/dev/null; echo "$l"; } | crontab -; echo "added: $l"; }
    done
    ;;
  remove)
    crontab -l 2>/dev/null | grep -vF "$B/" | crontab -
    for f in "$HOME/.profile" "$HOME/.bashrc"; do
      [ -f "$f" ] && grep -q "$MARK" "$f" && sed -i.bak "/# $MARK\$/,/# $MARK-end\$/d" "$f" && rm -f "$f.bak"
    done
    echo "removed all lines under $B/ and the shell-start hook"
    ;;
  hook)
    for f in "$HOME/.profile" "$HOME/.bashrc"; do
      [ -f "$f" ] || continue
      grep -q "$MARK" "$f" 2>/dev/null && continue
      printf '\n# %s\n%s\n# %s-end\n' "$MARK" "$HOOK_LINE" "$MARK" >> "$f"
    done
    ;;
  *) echo "usage: $0 install|status|remove|hook"; exit 2;;
esac
