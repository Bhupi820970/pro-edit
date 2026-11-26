#!/usr/bin/env bash
# Update the box copy of the code ONLY from origin/main and ONLY if every new commit is authored by the owner.
# Usage: bash scripts/box_update.sh        (never run automatically)
set -euo pipefail
OWNER=Bhupi820970
REPO=Bhupi820970/pro-edit
cd "$(dirname "$0")/.."
# remote may be called "origin" or "pro" depending on how the box copy was cloned
REMOTE=$(git remote | grep -Ex 'origin|pro' | head -n1); REMOTE=${REMOTE:-origin}
timeout 120 git fetch -q "$REMOTE" main
NEW=$(git rev-list "HEAD..$REMOTE/main")
[ -z "$NEW" ] && { echo "already up to date"; exit 0; }
for c in $NEW; do
  login=$(timeout 60 gh api "repos/$REPO/commits/$c" --jq '.author.login // ""')
  if [ "$login" != "$OWNER" ]; then echo "REFUSING: commit $c authored by '$login' (not $OWNER)"; exit 1; fi
done
git merge -q --ff-only "$REMOTE/main" && echo "updated to $(git rev-parse --short HEAD) (all commits by $OWNER)"

