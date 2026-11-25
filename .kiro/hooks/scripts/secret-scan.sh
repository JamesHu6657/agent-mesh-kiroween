#!/usr/bin/env bash
# secret-scan: fail-closed secret scan of staged changes or of a directory tree.
#
# Usage (from anywhere; the script cds to the repo root itself):
#   bash .kiro/hooks/scripts/secret-scan.sh              # staged changes (git index), before a commit
#   bash .kiro/hooks/scripts/secret-scan.sh --dir .kiro  # a directory tree, e.g. the Kiro config
#
# Layers (same tool, baseline and patterns as scripts/coord/sync-github.sh and push-review.sh):
#   1. gitleaks ($GITLEAKS, else PATH, else an own non-symlink /tmp/gitleaks), --redact, with an
#      optional local baseline file if present ($GITLEAKS_BASELINE).
#   2. The coordinator's extra pattern scan (shared pattern, scripts/coord/secret-pattern.sh).
#   3. Absolute home paths (/home/<user>/, the root home, /Users/<user>/): for the whole tree in
#      --dir mode; in staged mode only for staged files under .kiro/ (nothing personal under .kiro/).
# Findings are reported as file:line; matched secret text is never printed.
# Exit: 0 clean, 1 findings, 2 tool missing, scan error, or usage error (fail closed).

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

# Full review P1: never prefer a /tmp binary another user could plant; GITLEAKS env,
# then PATH, then /tmp/gitleaks only if owned by us and not a symlink (PR41 r2 P3).
GL="${GITLEAKS:-$(command -v gitleaks 2>/dev/null || true)}"
if [ -z "$GL" ] && [ ! -L /tmp/gitleaks ] && [ -f /tmp/gitleaks ] && [ -x /tmp/gitleaks ] && [ -O /tmp/gitleaks ]; then GL=/tmp/gitleaks; fi
if [ -z "$GL" ] || [ ! -x "$GL" ]; then
  echo "secret-scan: REFUSED: gitleaks not found (GITLEAKS env, PATH or own /tmp/gitleaks)"
  exit 2
fi
BASELINE="${GITLEAKS_BASELINE:-}"
BASELINE_ARGS=()
if [ -n "$BASELINE" ] && [ -f "$BASELINE" ]; then
  BASELINE_ARGS=(--baseline-path "$BASELINE")
fi

# Shared pattern (Sakana PR #4 P1-2: generic *_API_KEY=); fail closed if missing.
. scripts/coord/secret-pattern.sh 2>/dev/null && [ -n "${P:-}" ] || { echo "secret-scan: REFUSED: scripts/coord/secret-pattern.sh missing"; exit 2; }
# Written so that this file does not match itself.
HOMEP='(/home/[A-Za-z0-9._-]+/|/roo[t]/|/Users/[A-Za-z0-9._-]+/)'

OUT="$(mktemp /tmp/mesh-secret-scan-XXXXXX)" || exit 2
BLOB="$(mktemp /tmp/mesh-secret-blob-XXXXXX)" || { rm -f "$OUT"; exit 2; }
FILE_LIST="$(mktemp /tmp/mesh-secret-files-XXXXXX)" || { rm -f "$OUT" "$BLOB"; exit 2; }
trap 'rm -f "$OUT" "$BLOB" "$FILE_LIST"' EXIT
findings=0
scan_err=0

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

run_grep_list() {
  local pat="$1" batch=() f rc=1 r out
  while IFS= read -r -d '' f; do
    batch+=("$f")
    if [ "${#batch[@]}" -ge 500 ]; then
      out="$(grep -naHoE "$pat" -- "${batch[@]}")"
      r=$?
      [ -n "$out" ] && printf '%s\n' "$out"
      [ "$r" -gt 1 ] && rc="$r"
      [ "$r" -eq 0 ] && [ "$rc" -le 1 ] && rc=0
      batch=()
    fi
  done
  if [ "${#batch[@]}" -gt 0 ]; then
    out="$(grep -naHoE "$pat" -- "${batch[@]}")"
    r=$?
    [ -n "$out" ] && printf '%s\n' "$out"
    [ "$r" -gt 1 ] && rc="$r"
    [ "$r" -eq 0 ] && [ "$rc" -le 1 ] && rc=0
  fi
  return "$rc"
}

if [ "$MODE" = staged ]; then
  if git diff --cached --quiet; then
    echo "secret-scan: nothing staged"
    exit 0
  fi
  "$GL" git --staged --no-banner --redact "${BASELINE_ARGS[@]}" . >"$OUT" 2>&1
  report_gitleaks "$?" "staged"

  # Linear full review D8: NUL-delimited names (no C-quoting), and a staged blob that cannot
  # be read is a finding, never an empty pass.
  while IFS= read -r -d '' f; do
    [ -n "$f" ] || continue
    if ! git show ":$f" >"$BLOB" 2>/dev/null; then
      echo "secret-scan: cannot read staged $f"
      findings=$((findings + 1)); continue
    fi
    lines="$(grep -naE "$P" -- "$BLOB" | cut -d: -f1 | tr '\n' ' ')"
    rc=$?
    if [ "$rc" -gt 1 ]; then
      echo "secret-scan: pattern scan failed on staged $f (grep rc $rc)"
      scan_err=1
    fi
    if [ -n "$lines" ]; then
      echo "secret-scan: pattern hit in staged $f at line(s) $lines"
      findings=$((findings + 1))
    fi
    case "$f" in
      .kiro/agents/*) ;;  # agent permission paths (allowedPaths) are not secrets
      .kiro/*)
        homes="$(grep -naoE "$HOMEP" -- "$BLOB" | tr '\n' ' ')"
        rc=$?
        if [ "$rc" -gt 1 ]; then
          echo "secret-scan: absolute home path scan failed on staged $f (grep rc $rc)"
          scan_err=1
        fi
        if [ -n "$homes" ]; then
          echo "secret-scan: absolute home path in staged $f: $homes"
          findings=$((findings + 1))
        fi
        ;;
    esac
  done < <(git diff --cached --name-only -z --diff-filter=ACMR)
else
  "$GL" dir --no-banner --redact "${BASELINE_ARGS[@]}" "$DIR" >"$OUT" 2>&1
  report_gitleaks "$?" "dir $DIR"

  pat="$(grep -rnaE "$P" -- "$DIR" | cut -d: -f1,2)"
  pat_rc=$?
  if [ "$pat_rc" -gt 1 ]; then
    echo "secret-scan: pattern scan failed (dir $DIR, grep rc $pat_rc)"
    scan_err=1
  fi
  if [ -n "$pat" ]; then
    echo "secret-scan: pattern hits (file:line):"
    printf '%s\n' "$pat"
    findings=$((findings + 1))
  elif [ "$pat_rc" -le 1 ]; then
    echo "secret-scan: pattern scan clean (dir $DIR)"
  fi

  # .kiro/agents allowedPaths are permission paths, not secrets; scope exclusion to .kiro/agents/* only.
  if ! DIR_ABS="$(cd "$DIR" && pwd -P)"; then
    echo "secret-scan: cannot resolve dir $DIR"
    scan_err=1
  else
    case "$DIR_ABS" in
      "$ROOT"|"$ROOT"/*) excl="$ROOT/.kiro/agents" ;;
      */.kiro) excl="$DIR_ABS/agents" ;;
      *) excl="$DIR_ABS/.kiro/agents" ;;
    esac
    find "$DIR_ABS" -path "$excl" -prune -o -type f -print0 >"$FILE_LIST"
    find_rc=$?
    if [ "$find_rc" -ne 0 ]; then
      echo "secret-scan: absolute home path walk failed (dir $DIR, find rc $find_rc)"
      scan_err=1
    fi
    homes=""
    homes_rc=1
    if [ -s "$FILE_LIST" ]; then
      homes="$(run_grep_list "$HOMEP" <"$FILE_LIST")"
      homes_rc=$?
    fi
    if [ "$homes_rc" -gt 1 ]; then
      echo "secret-scan: absolute home path scan failed (dir $DIR, grep rc $homes_rc)"
      scan_err=1
    fi
    if [ -n "$homes" ]; then
      echo "secret-scan: absolute home paths (file:line:path):"
      printf '%s\n' "$homes"
      findings=$((findings + 1))
    elif [ "$find_rc" -eq 0 ] && [ "$homes_rc" -le 1 ]; then
      echo "secret-scan: no absolute home paths (dir $DIR)"
    fi
  fi
fi

if [ "$findings" -gt 0 ]; then
  echo "secret-scan: FAIL ($findings finding group(s))"
  exit 1
fi
if [ "$scan_err" -gt 0 ]; then
  echo "secret-scan: FAIL (scan error)"
  exit 2
fi
echo "secret-scan: OK"
exit 0
