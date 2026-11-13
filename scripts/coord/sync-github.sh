#!/usr/bin/env bash
# Auto-sync main to the private GitHub review repo (James 2026-11-09 19:46).
# Run only after a merge into main whose full suite is green. Fast-forward only; no force, no wt/*, no tags.
# Scans only the new commits (github/main..main) with gitleaks + pattern scan; refuses to push on any hit.
set -euo pipefail
cd "$(dirname "$0")/../.."
[ "$(git branch --show-current)" = main ] || { echo "REFUSED: not on main"; exit 2; }
git fetch -q github main
R="github/main..main"
git merge-base --is-ancestor github/main main || { echo "REFUSED: not a fast-forward"; exit 2; }
N=$(git rev-list --count $R); [ "$N" -gt 0 ] || { echo "nothing to push"; exit 0; }
GL=/tmp/gitleaks; [ -x $GL ] || { echo "REFUSED: gitleaks missing at $GL"; exit 2; }
# Known fake test fixtures are allowlisted by fingerprint in scripts/coord/gitleaks-baseline.json
if ! $GL git --no-banner --redact --log-opts="$R" --baseline-path scripts/coord/gitleaks-baseline.json . > /tmp/sync-gl.txt 2>&1; then
  echo "REFUSED: gitleaks hits in new commits:"; grep -E "RuleID|File|Commit" /tmp/sync-gl.txt | head -30; exit 3; fi
P='[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16}|brd-customer-[a-z0-9_-]+|sk-(ant-|proj-)?[A-Za-z0-9_-]{24,}|(HOSTA_API_KEY|BRIGHTDATA_[A-Z_]+|HERMES_TELEGRAM_BOT_TOKEN|MWAPI_API_KEY|WECHAT_ROUTINE_WEBHOOK_KEY)=[^` "\\]{6,}|BEGIN (OPENSSH|EC|DSA) PRIVATE KEY'
H=$(git log -p $R | grep -E '^\+' | grep -cE "$P" || true)
[ "$H" = 0 ] || { echo "REFUSED: $H pattern hits in new commits"; exit 3; }
git push github main:main
S=$(git rev-parse main); RS=$(git ls-remote github refs/heads/main | cut -f1)
[ "$S" = "$RS" ] || { echo "WARN remote head $RS != $S"; exit 4; }
echo "pushed $N commits, github main = $S"
