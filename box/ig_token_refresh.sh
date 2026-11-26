#!/usr/bin/env bash
# Weekly IG token refresh, no new secrets. Plain script, no AI, executes nothing from the repo/artifacts:
#  1. dispatch ig-token.yml (mode=refresh) with the owner's gh login and wait for it;
#  2. download its encrypted artifact, decrypt with the box-only key (/workspace/.secrets/ig_handoff_rsa.pem)
#     and pipe the token straight into `gh secret set IG_ACCESS_TOKEN` (stdin; never echoed, never on disk
#     in clear), then delete the artifact;
#  3. record the outcome (no secret) in repo variable IG_TOKEN_REFRESH, which the watchdog health check
#     turns into a health-issue line when it starts with "failed".
#   crontab (box clock = IST):  17 3 * * 0
set -u
umask 077
REPO=Bhupi820970/pro-edit
RUN=/workspace/gta6/box_fetch
LOG="$RUN/ig_token_refresh.log"
KEY=/workspace/.secrets/ig_handoff_rsa.pem
HERE="$(cd "$(dirname "$0")" && pwd)"
export PATH="/usr/local/bin:/usr/bin:/bin"
export GH_CONFIG_DIR=/workspace/.ghnew
mkdir -p "$RUN"
log() { echo "$(date '+%F %T %Z') $*" >> "$LOG"; }
status() { timeout 60 gh variable set IG_TOKEN_REFRESH --repo "$REPO" --body "$1 $(date '+%F %H:%M IST')" >/dev/null 2>&1; log "$1"; }
[ -r "$KEY" ] || { status "failed: box key missing"; exit 1; }
T0=$(date -u +%Y-%m-%dT%H:%M:%SZ)
timeout 60 gh workflow run ig-token.yml --repo "$REPO" --ref main -f mode=refresh >/dev/null 2>&1 \
  || { status "failed: could not dispatch ig-token.yml"; exit 1; }
RID=""
for i in $(seq 1 20); do
  sleep 6
  RID=$(timeout 60 gh run list --repo "$REPO" --workflow ig-token.yml --event workflow_dispatch -L 5 \
        --json databaseId,createdAt -q "[.[] | select(.createdAt >= \"$T0\")][-1].databaseId" 2>/dev/null)
  [[ "$RID" =~ ^[0-9]{5,20}$ ]] && break
done
[[ "$RID" =~ ^[0-9]{5,20}$ ]] || { status "failed: run not found"; exit 1; }
for i in $(seq 1 60); do
  ST=$(timeout 60 gh run view "$RID" --repo "$REPO" --json status,conclusion -q '.status+" "+.conclusion' 2>/dev/null)
  case "$ST" in completed*) break;; esac
  sleep 10
done
case "$ST" in
  "completed success") ;;
  *) status "failed: ig-token run $RID ended '$ST' (token not refreshable or invalid; see run summary)"; exit 1;;
esac
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
if ! timeout 120 gh run download "$RID" --repo "$REPO" -n ig-handoff -D "$TMP" >/dev/null 2>&1; then
  status "ok: checked, nothing to refresh (run $RID; Facebook-Login token or never-expiring)"; exit 0
fi
[ -f "$TMP/ig_handoff.bin" ] || { status "failed: handoff file missing"; exit 1; }
if python3 "$HERE/ig_handoff_decrypt.py" "$TMP/ig_handoff.bin" "$KEY" \
     | timeout 60 gh secret set IG_ACCESS_TOKEN --repo "$REPO" >/dev/null 2>&1; then
  status "ok: refreshed (run $RID)"
else
  status "failed: decrypt or secret update failed (run $RID)"
fi
AID=$(timeout 60 gh api "repos/$REPO/actions/runs/$RID/artifacts" -q '.artifacts[]|select(.name=="ig-handoff")|.id' 2>/dev/null)
[[ "$AID" =~ ^[0-9]{3,20}$ ]] && timeout 60 gh api -X DELETE "repos/$REPO/actions/artifacts/$AID" >/dev/null 2>&1
exit 0
