#!/usr/bin/env bash
# secret-scan: fail-closed secret scan of staged changes or of a directory tree.
#
# Usage (from anywhere; the script cds to the repo root itself):
#   bash .kiro/hooks/scripts/secret-scan.sh              # staged changes (git index), before a commit
#   bash .kiro/hooks/scripts/secret-scan.sh --dir .kiro  # a directory tree, e.g. the Kiro config
#
# Layers (same tool, baseline and patterns as scripts/coord/sync-github.sh and push-review.sh):
#   1. gitleaks (/tmp/gitleaks, else gitleaks on PATH), --redact, baseline
#      scripts/coord/gitleaks-baseline.json (it allowlists the known fake test fixtures by
#      commit fingerprint, so it only matches committed history, not staged or dir scans).
#   2. The coordinator's extra pattern scan (pattern copied verbatim from sync-github.sh).
#   3. Absolute home paths (/home/<user>/, the root home, /Users/<user>/): for the whole tree in
#      --dir mode; in staged mode only for staged files under .kiro/ (nothing personal under .kiro/).
# Findings are reported as file:line; matched secret text is never printed.
# Exit: 0 clean, 1 findings, 2 tool missing or usage error (fail closed).

set -o pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd -P)" || exit 2
cd "$ROOT" || exit 2

usage() { echo "secret-scan: usage: secret-scan.sh [--dir PATH]"; }

MODE=staged
DIR=""
case "${1:-}" in
  "") ;;
  --dir)
    MODE=dir
    DIR="${2:-}"
    if [ -z "$DIR" ] || [ ! -d "$DIR" ]; then usage; exit 2; fi
    ;;
  -h|--help) usage; exit 0 ;;
  *) usage; exit 2 ;;
esac

GL=""
if [ -x /tmp/gitleaks ]; then
  GL=/tmp/gitleaks
elif command -v gitleaks >/dev/null 2>&1; then
  GL="$(command -v gitleaks)"
fi
if [ -z "$GL" ]; then
  echo "secret-scan: REFUSED: gitleaks not found (/tmp/gitleaks or PATH)"
  exit 2
fi
BASELINE=scripts/coord/gitleaks-baseline.json
if [ ! -f "$BASELINE" ]; then
  echo "secret-scan: REFUSED: $BASELINE missing"
  exit 2
fi

# Verbatim from scripts/coord/sync-github.sh (variable P).
P='[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16}|brd-customer-[a-z0-9_-]+|sk-(ant-|proj-)?[A-Za-z0-9_-]{24,}|(AGENT37_API_KEY|BRIGHTDATA_[A-Z_]+|HERMES_TELEGRAM_BOT_TOKEN|MWAPI_API_KEY|WECHAT_ROUTINE_WEBHOOK_KEY)=[^` "\\]{6,}|BEGIN (OPENSSH|EC|DSA) PRIVATE KEY'
# Written so that this file does not match itself.
HOMEP='(/home/[A-Za-z0-9._-]+/|/roo[t]/|/Users/[A-Za-z0-9._-]+/)'

OUT="$(mktemp /tmp/mesh-secret-scan-XXXXXX)" || exit 2
trap 'rm -f "$OUT"' EXIT
findings=0

report_gitleaks() {
  # $1 = gitleaks rc, $2 = label
  if [ "$1" -eq 0 ]; then
    echo "secret-scan: gitleaks clean ($2)"
  else
    echo "secret-scan: gitleaks rc $1 ($2): findings or scan error (output is redacted):"
    grep -E 'RuleID|File|Line|Fingerprint|ERR|FTL|error' "$OUT" | head -40
    findings=$((findings + 1))
  fi
}

if [ "$MODE" = staged ]; then
  if git diff --cached --quiet; then
    echo "secret-scan: nothing staged"
    exit 0
  fi
  "$GL" git --staged --no-banner --redact --baseline-path "$BASELINE" . >"$OUT" 2>&1
  report_gitleaks "$?" "staged"

  while IFS= read -r f; do
    [ -n "$f" ] || continue
    lines="$(git show ":$f" 2>/dev/null | grep -nIE "$P" | cut -d: -f1 | tr '\n' ' ')"
    if [ -n "$lines" ]; then
      echo "secret-scan: pattern hit in staged $f at line(s) $lines"
      findings=$((findings + 1))
    fi
    case "$f" in
      .kiro/agents/*) ;;  # agent permission paths (allowedPaths) are not secrets
      .kiro/*)
        homes="$(git show ":$f" 2>/dev/null | grep -nIoE "$HOMEP" | tr '\n' ' ')"
        if [ -n "$homes" ]; then
          echo "secret-scan: absolute home path in staged $f: $homes"
          findings=$((findings + 1))
        fi
        ;;
    esac
  done < <(git diff --cached --name-only --diff-filter=ACMR)
else
  "$GL" dir --no-banner --redact --baseline-path "$BASELINE" "$DIR" >"$OUT" 2>&1
  report_gitleaks "$?" "dir $DIR"

  pat="$(grep -rnIE "$P" -- "$DIR" | cut -d: -f1,2)"
  if [ -n "$pat" ]; then
    echo "secret-scan: pattern hits (file:line):"
    printf '%s\n' "$pat"
    findings=$((findings + 1))
  else
    echo "secret-scan: pattern scan clean (dir $DIR)"
  fi

  homes="$(grep -rnIoE "$HOMEP" --exclude-dir=agents -- "$DIR")"  # .kiro/agents allowedPaths are permission paths, not secrets
  if [ -n "$homes" ]; then
    echo "secret-scan: absolute home paths (file:line:path):"
    printf '%s\n' "$homes"
    findings=$((findings + 1))
  else
    echo "secret-scan: no absolute home paths (dir $DIR)"
  fi
fi

if [ "$findings" -gt 0 ]; then
  echo "secret-scan: FAIL ($findings finding group(s))"
  exit 1
fi
echo "secret-scan: OK"
exit 0
