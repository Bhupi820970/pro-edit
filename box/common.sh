#!/usr/bin/env bash
# Shared settings + portable helpers for the box scripts (Linux or macOS). Sourced by box/*.sh only.
# Local settings come from box/local.env (NOT in git, see box/local.env.example) and, for older setups,
# $VICE_DATA_DIR/box.env. Both files are PARSED (KEY=value, whitelisted keys, plain path/name values),
# never sourced or executed.
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
_vc_load() {
  [ -f "$1" ] || return 0
  while IFS='=' read -r k v; do
    case "$k" in
      VICE_DATA_DIR|VICE_BOX_ROOT|VICE_PIPELINE_HOME|VICE_REPO|VICE_IG_HANDOFF_KEY|GH_CONFIG_DIR|\
      VICE_YT_COOKIES_BROWSER|VICE_YTDLP_JS_RUNTIME|BOX_FETCH_MAX_PER_CAT|BOX_FETCH_MIN_FREE_GB|\
      BOX_FETCH_SIBLINGS|BOX_FETCH_OVERLAY_CHECK|BOX_FETCH_MIN_HEIGHT)
        if [[ "$v" =~ ^[A-Za-z0-9:/._+@-]{1,200}$ ]]; then export "$k=$v"; fi ;;
    esac
  done < "$1"
}
_vc_load "$REPO_DIR/box/local.env"
export VICE_DATA_DIR="${VICE_DATA_DIR:-$HOME/vice-cartel/box_fetch}"
_vc_load "$VICE_DATA_DIR/box.env"
export VICE_BOX_ROOT="${VICE_BOX_ROOT:-$(dirname "$VICE_DATA_DIR")}"
export VICE_PIPELINE_HOME="${VICE_PIPELINE_HOME:-$VICE_BOX_ROOT/pipeline}"
export VICE_REPO="${VICE_REPO:-Bhupi820970/pro-edit}"
export VICE_IG_HANDOFF_KEY="${VICE_IG_HANDOFF_KEY:-$HOME/.config/vice-cartel/ig_handoff_rsa.pem}"
export BOX_FETCH_DIR="$VICE_DATA_DIR" VICE_BOX_FETCH_DIR="$VICE_DATA_DIR"
export PATH="$HOME/.local/bin:$HOME/.deno/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export PYTHONUNBUFFERED=1
RUN="$VICE_DATA_DIR"
mkdir -p "$RUN"

# file size in bytes (GNU stat / BSD stat)
fsize() { stat -c %s "$1" 2>/dev/null || stat -f %z "$1" 2>/dev/null || echo 0; }
# rotate a log above N bytes
rotate_log() { [ -f "$1" ] && [ "$(fsize "$1")" -gt "$2" ] && mv -f "$1" "$1.1"; return 0; }
# timeout (GNU), gtimeout (Homebrew coreutils on macOS), else run without a cap
tmo() {
  if command -v timeout >/dev/null 2>&1; then timeout "$@"
  elif command -v gtimeout >/dev/null 2>&1; then gtimeout "$@"
  else shift; "$@"; fi
}
# low CPU / IO priority where available
# lowprio is a shell function: never pass it to timeout/tmo (timeout runs only real programs)
lowprio() { if command -v ionice >/dev/null 2>&1; then nice -n 10 ionice -c3 "$@"; else nice -n 10 "$@"; fi; }
# single-instance lock: flock on Linux, mkdir lock elsewhere. Returns 1 if already held.
take_lock() {
  if command -v flock >/dev/null 2>&1; then
    exec 9>"$1"; flock -n 9 || return 1
  else
    if mkdir "$1.d" 2>/dev/null; then trap 'rmdir "$1.d" 2>/dev/null' EXIT
    else
      # stale lock older than 4 h -> take over
      if [ -n "$(find "$1.d" -maxdepth 0 -mmin +240 2>/dev/null)" ]; then rmdir "$1.d"; mkdir "$1.d" || return 1
      else return 1; fi
    fi
  fi
  return 0
}
