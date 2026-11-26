#!/usr/bin/env bash
# One-time setup of the pro-edit box side on a new Linux or macOS computer. Safe to re-run.
# It does NOT install the cron lines (so two computers never run them at once); see HANDOFF.md step 6.
set -u
B="$(cd "$(dirname "$0")" && pwd)"
R="$(dirname "$B")"
ok=1
for t in git python3 ffmpeg ffprobe gh; do
  command -v "$t" >/dev/null 2>&1 && echo "ok   $t" || { echo "MISSING $t"; ok=0; }
done
command -v deno >/dev/null 2>&1 && echo "ok   deno" || echo "warn deno missing (yt-dlp needs a JS runtime for YouTube: https://deno.land)"
command -v flock >/dev/null 2>&1 || echo "note flock missing (macOS): a mkdir lock is used instead"
command -v timeout >/dev/null 2>&1 || command -v gtimeout >/dev/null 2>&1 || echo "note no timeout/gtimeout (macOS: brew install coreutils)"
[ "$ok" = 1 ] || { echo "install the missing tools first (Linux: apt, macOS: brew)"; exit 1; }
python3 -m pip install --user -q -r "$B/requirements-box.txt" || python3 -m pip install --user --break-system-packages -q -r "$B/requirements-box.txt"
[ -f "$B/local.env" ] || { cp "$B/local.env.example" "$B/local.env"; sed -i.bak "s#/home/YOUR_USER#$HOME#g" "$B/local.env" && rm -f "$B/local.env.bak"; echo "created box/local.env (edit it)"; }
. "$B/common.sh"
mkdir -p "$VICE_DATA_DIR"
gh auth status >/dev/null 2>&1 && echo "ok   gh login" || echo "run: gh auth login   (account that owns $VICE_REPO; needs repo + workflow scopes)"
# IG token handoff key pair: the private key stays on this computer only; the public key is in git.
if [ ! -f "$VICE_IG_HANDOFF_KEY" ]; then
  mkdir -p "$(dirname "$VICE_IG_HANDOFF_KEY")"; ( umask 077; openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out "$VICE_IG_HANDOFF_KEY" 2>/dev/null )
  openssl pkey -in "$VICE_IG_HANDOFF_KEY" -pubout -out "$R/config/ig_handoff_pub.pem"
  echo "new IG handoff key pair: commit config/ig_handoff_pub.pem (public) -> git add -f config/ig_handoff_pub.pem"
fi
echo "setup done. Next: edit box/local.env, then (on ONE computer only) bash box/ensure_cron.sh install"
