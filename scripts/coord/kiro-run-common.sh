#!/usr/bin/env bash
# Shared runner for on-demand Kiro reviewer/planner sessions (fresh session each time, scoped profile mesh-reviewer).
# Refuses to run while another Kiro reviewer/planner run is active.
set -euo pipefail
cd "$(dirname "$0")/../.."
K=$HOME/.local/bin/kiro-cli
run_kiro() { # $1=out-file(.md) $2=prompt-text $3=log-tag
  local OUT="$1" PROMPT="$2" TAG="$3" TS LOG
  if pgrep -f 'kiro-cli chat .*--agent mesh-reviewer' >/dev/null; then echo "REFUSED: a mesh-reviewer run is active"; exit 3; fi
  [ -e "$OUT" ] && { echo "REFUSED: $OUT exists"; exit 2; }
  mkdir -p "$(dirname "$OUT")" logs/raw
  TS=$(date +%Y%m%d-%H%M%S); LOG=logs/raw/kiro-$TAG-$TS.jsonl
  unset FACTORY_API_KEY FACTORY_TOKEN
  echo "kiro $TAG start $(date '+%F %T') log=$LOG out=$OUT"
  set +e
  timeout "${KIRO_TIMEOUT:-5400}" "$K" chat --no-interactive --agent mesh-reviewer --model claude-opus-5.5 --effort high \
    --output-format stream-json "$PROMPT" > "$LOG" 2> "${LOG%.jsonl}.err" < /dev/null
  local rc=$?; set -e
  echo "kiro $TAG end rc=$rc $(date '+%F %T')"
  if [ -s "$OUT" ]; then echo "OUTPUT $OUT"; head -1 "$OUT"; else echo "NO OUTPUT FILE (see $LOG / ${LOG%.jsonl}.err)"; exit 4; fi
}
