#!/usr/bin/env bash
# RETIRED 2026-11-26: the old nohup `box_fetch.py loop` service is off for good.
# The box crontab entry `50 1-23/3 * * * .../box/fetch_sources.sh` is the only fetcher; this script never
# starts, restarts or installs anything (no nohup, no profile hook, no @reboot).
echo "retired: crontab fetcher (box/fetch_sources.sh) is the only fetcher"
exit 0
