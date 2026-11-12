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
R="github/main..$B"
GL=/tmp/gitleaks; [ -x $GL ] || { echo "REFUSED: gitleaks missing"; exit 2; }
if ! $GL git --no-banner --redact --log-opts="$R" --baseline-path scripts/coord/gitleaks-baseline.json . > /tmp/pr-gl.txt 2>&1; then
  echo "REFUSED: gitleaks hits:"; grep -E "RuleID|File|Commit" /tmp/pr-gl.txt | head -30; exit 3; fi
P='[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16}|brd-customer-[a-z0-9_-]+|sk-(ant-|proj-)?[A-Za-z0-9_-]{24,}|(AGENT37_API_KEY|BRIGHTDATA_[A-Z_]+|HERMES_TELEGRAM_BOT_TOKEN|MWAPI_API_KEY|WECHAT_ROUTINE_WEBHOOK_KEY)=[^` "\\]{6,}|BEGIN (OPENSSH|EC|DSA) PRIVATE KEY'
H=$(git log -p $R | grep -E '^\+' | grep -cE "$P" || true)
[ "$H" = 0 ] || { echo "REFUSED: $H pattern hits"; exit 3; }
git push github "$B:refs/heads/review/$N"
echo "branch review/$N base $(git merge-base github/main $B) (github main $(git rev-parse github/main)) head $(git rev-parse $B) commits $(git rev-list --count $R)"
