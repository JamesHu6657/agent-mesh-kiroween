#!/usr/bin/env bash
# Push a finished wt/<run> branch to the private GitHub repo as review/<run> for a Linear Agent PR review
# (James 2026-11-09 20:23). Scans only the branch's new commits vs github/main; no force; refuses on any hit.
# Usage: bash scripts/coord/push-review.sh <run>        (e.g. m3-r4)   | --delete <run> to remove the remote branch
set -euo pipefail
cd "$(dirname "$0")/../.."
if [ "${1:-}" = "--delete" ]; then git push github --delete "review/${2:?run}"; exit 0; fi
N="${1:?run}"; [[ "$N" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "bad run"; exit 2; }
git fetch -q github main
B="wt/$N"; git rev-parse -q --verify "$B" >/dev/null || { echo "no branch $B"; exit 2; }
# Linear full review D2: resolve both ends once; scan and push exactly these SHAs, so a commit
# landing on the branch during the scan is never pushed unscanned.
S=$(git rev-parse "$B^{commit}"); BASE=$(git rev-parse github/main^{commit})
R="$BASE..$S"
GL=${GITLEAKS:-/tmp/gitleaks}; [ -x $GL ] || { echo "REFUSED: gitleaks missing"; exit 2; }
if ! $GL git --no-banner --redact --log-opts="$R" --baseline-path scripts/coord/gitleaks-baseline.json . > /tmp/pr-gl.txt 2>&1; then
  echo "REFUSED: gitleaks hits:"; grep -E "RuleID|File|Commit" /tmp/pr-gl.txt | head -30; exit 3; fi
# Shared pattern (Sakana PR #4 P1-2: generic *_API_KEY=); fail closed if missing.
. scripts/coord/secret-pattern.sh 2>/dev/null && [ -n "${P:-}" ] || { echo "REFUSED: scripts/coord/secret-pattern.sh missing"; exit 2; }
H=$(git log -p $R | grep -E '^\+' | grep -cE "$P" || true)
[ "$H" = 0 ] || { echo "REFUSED: $H pattern hits"; exit 3; }
git push github "$S:refs/heads/review/$N"
echo "branch review/$N base $(git merge-base $BASE $S) (github main $BASE) head $S commits $(git rev-list --count $R)"
