#!/usr/bin/env bash
# Restarts the box_fetch service if it died, and starts it after a box reset.
# This box has no systemd as PID 1 and no cron package, so a small nohup loop
# + login/interactive shell hooks (install) is what actually comes back after reboot.
# start is idempotent and does not restart a healthy service.
set -u
DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE="$DIR/scripts/box_fetch_service.sh"
RUN=/workspace/gta6/box_fetch; mkdir -p "$RUN"
PID="$RUN/box_fetch_watchdog.pid"
LOG="$RUN/box_fetch_watchdog.log"
INTERVAL="${BOX_FETCH_WATCHDOG_INTERVAL:-900}"
# shell hooks (same pattern as /workspace/scanners/bin/ensure-cron.sh)
HOOK_MARK="vice-cartel-box-fetch-autostart"
HOOK_LINE='[ -x /workspace/gta6/vicestyle/scripts/box_fetch_watchdog.sh ] && /workspace/gta6/vicestyle/scripts/box_fetch_watchdog.sh start >/dev/null 2>&1'
export PATH="/home/box/.local/bin:/home/box/.deno/bin:/usr/local/bin:/usr/bin:/bin${PATH:+:$PATH}"

running() { [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null; }

install_hooks() {
  local f
  local begin="# $HOOK_MARK"
  local end="# $HOOK_MARK-end"
  for f in /home/box/.profile /home/box/.bashrc; do
    [ -f "$f" ] || continue
    if grep -q "$HOOK_MARK" "$f" 2>/dev/null; then
      continue
    fi
    printf '\n%s\n%s\n%s\n' "$begin" "$HOOK_LINE" "$end" >> "$f"
    echo "hooked $f"
  done
  mkdir -p /home/box/.config/autostart
  cat > /home/box/.config/autostart/vice-cartel-box-fetch.desktop <<DESK
[Desktop Entry]
Type=Application
Name=Vice Cartel box fetch
Comment=Start the box_fetch service after a desktop session starts
Exec=/workspace/gta6/vicestyle/scripts/box_fetch_watchdog.sh start
X-GNOME-Autostart-enabled=true
Hidden=false
NoDisplay=true
DESK
}

case "${1:-status}" in
  start)
    install_hooks
    if running; then echo "watchdog already running (pid $(cat "$PID"))"; exit 0; fi
    nohup bash -c "while true; do bash '$SERVICE' start >> '$LOG' 2>&1; sleep $INTERVAL; done" \
      >> "$LOG" 2>&1 &
    echo $! > "$PID"
    echo "watchdog started (pid $!), every ${INTERVAL}s, log: $LOG";;
  stop)
    if running; then kill "$(cat "$PID")" 2>/dev/null; rm -f "$PID"; echo "watchdog stopped"; else echo "watchdog not running"; fi;;
  status)
    if running; then echo "watchdog running (pid $(cat "$PID")) interval=${INTERVAL}s"; else echo "watchdog not running"; fi
    bash "$SERVICE" status;;
  install)
    install_hooks
    echo "hooks installed";;
  *) echo "usage: $0 start|stop|status|install"; exit 1;;
esac

