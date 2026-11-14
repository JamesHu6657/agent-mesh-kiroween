#!/usr/bin/env bash
# Auto-sync main to the private GitHub review repo (James 2026-11-09 19:46).
# Run only after a merge into main whose full suite is green. Fast-forward only; no force, no wt/*, no tags.
# Scans only the new commits (github/main..main) with gitleaks + pattern scan; refuses to push on any hit.
set -euo pipefail
cd "$(dirname "$0")/../.."
[ "$(git branch --show-current)" = main ] || { echo "REFUSED: not on main"; exit 2; }
git fetch -q github main
# Linear full review D2: resolve both ends once; scan and push exactly these SHAs.
S=$(git rev-parse main^{commit}); BASE=$(git rev-parse github/main^{commit})
R="$BASE..$S"
git merge-base --is-ancestor "$BASE" "$S" || { echo "REFUSED: not a fast-forward"; exit 2; }
N=$(git rev-list --count $R); [ "$N" -gt 0 ] || { echo "nothing to push"; exit 0; }
GL=${GITLEAKS:-/tmp/gitleaks}; [ -x $GL ] || { echo "REFUSED: gitleaks missing at $GL"; exit 2; }
# Known fake test fixtures are allowlisted by fingerprint in scripts/coord/gitleaks-baseline.json
if ! $GL git --no-banner --redact --log-opts="$R" --baseline-path scripts/coord/gitleaks-baseline.json . > /tmp/sync-gl.txt 2>&1; then
  echo "REFUSED: gitleaks hits in new commits:"; grep -E "RuleID|File|Commit" /tmp/sync-gl.txt | head -30; exit 3; fi
# Shared pattern (Sakana PR #4 P1-2: generic *_API_KEY=); fail closed if missing.
. scripts/coord/secret-pattern.sh 2>/dev/null && [ -n "${P:-}" ] || { echo "REFUSED: scripts/coord/secret-pattern.sh missing"; exit 2; }
H=$(git log -p $R | grep -E '^\+' | grep -cE "$P" || true)
[ "$H" = 0 ] || { echo "REFUSED: $H pattern hits in new commits"; exit 3; }
git push github "$S:refs/heads/main"
RS=$(git ls-remote github refs/heads/main | cut -f1)
[ "$S" = "$RS" ] || { echo "WARN remote head $RS != $S"; exit 4; }
echo "pushed $N commits, github main = $S"
