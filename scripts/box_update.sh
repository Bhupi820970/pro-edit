#!/usr/bin/env bash
# Update the box copy of the code ONLY from origin/main and ONLY if every new commit is authored by the owner.
# Usage: bash scripts/box_update.sh        (never run automatically)
set -euo pipefail
OWNER=Bhupi820970
REPO=Bhupi820970/pro-edit
cd "$(dirname "$0")/.."
git fetch -q origin main
NEW=$(git rev-list HEAD..origin/main)
[ -z "$NEW" ] && { echo "already up to date"; exit 0; }
for c in $NEW; do
  login=$(gh api "repos/$REPO/commits/$c" --jq '.author.login // ""')
  if [ "$login" != "$OWNER" ]; then echo "REFUSING: commit $c authored by '$login' (not $OWNER)"; exit 1; fi
done
git merge -q --ff-only origin/main && echo "updated to $(git rev-parse --short HEAD) (all commits by $OWNER)"

