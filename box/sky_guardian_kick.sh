#!/bin/bash
# Kick Skywalker slot-guardian every 30 min (GitHub schedule runs are sparse). Only dispatches a workflow; runs no repo code.
LOG=/workspace/gta6/box_fetch/sky_guardian_kick.log
gh workflow run slot-guardian.yml -R Bhupi820970/skywalker-shorts >/dev/null 2>&1 && echo "$(date '+%F %T') kicked" >> "$LOG" || echo "$(date '+%F %T') FAIL kick" >> "$LOG"
tail -200 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
