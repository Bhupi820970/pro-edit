#!/usr/bin/env bash
# Box-side downloader loop for the Actions pipeline (no cron/systemd on this box).
# Start (idempotent):  bash /workspace/gta6/vicestyle/scripts/box_fetch_service.sh start
# After a box reset the watchdog (scripts/box_fetch_watchdog.sh) auto-starts this via
# ~/.profile + ~/.bashrc hooks and a 15-minute check. start never kills a healthy process.
set -u
export PATH="/home/box/.local/bin:/home/box/.deno/bin:/usr/local/bin:/usr/bin:/bin${PATH:+:$PATH}"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN=/workspace/gta6/box_fetch; mkdir -p "$RUN"
PID="$RUN/box_fetch.pid"; LOG="$RUN/box_fetch.log"
INTERVAL="${BOX_FETCH_INTERVAL:-3600}"
running() { [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null; }
case "${1:-status}" in
  start)
    if running; then echo "already running (pid $(cat "$PID"))"; exit 0; fi
    # supervisor: restarts the python loop if it ever exits
    nohup bash -c "while true; do python3 '$DIR/box_fetch.py' loop --interval $INTERVAL --cookies-from-browser chrome >> '$LOG' 2>&1; sleep 60; done" \
      > /dev/null 2>&1 &
    echo $! > "$PID"; echo "started (pid $!), log: $LOG";;
  stop)
    if running; then pkill -P "$(cat "$PID")"; kill "$(cat "$PID")"; rm -f "$PID"; echo stopped; else echo "not running"; fi;;
  status)
    if running; then echo "running (pid $(cat "$PID"))"; else echo "not running"; fi
    [ -f "$RUN/box_heartbeat.json" ] && python3 -c "import json;d=json.load(open('$RUN/box_heartbeat.json'));print('last_run', d.get('last_run'), 'pool', d.get('pool'))";;
  logs) tail -n 50 "$LOG";;
  watchdog)
    bash "$DIR/scripts/box_fetch_watchdog.sh" "${2:-status}";;
  *) echo "usage: $0 start|stop|status|logs|watchdog"; exit 1;;
esac

