#!/usr/bin/env bash
# Launch ONE detached agy implementation run with the fixed model and scoped session permissions.
# Usage: bash scripts/coord/dispatch-agy.sh prompts/<name>.md <run-name>
# Refuses to start if any agy run is already active (no parallel writers on this repo).
set -euo pipefail
cd "$(dirname "$0")/../.."
P="${1:?prompt file}"; N="${2:?run name}"
[[ "$P" =~ ^prompts/[A-Za-z0-9._-]+\.md$ ]] || { echo "bad prompt path"; exit 2; }
[[ "$N" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "bad run name"; exit 2; }
[ -f "$P" ] || { echo "missing $P"; exit 2; }
source "$(dirname "$0")/coder-guard.sh"; coder_guard
[ -e "logs/raw/agy-$N.jsonl" ] && { echo "REFUSED: run name exists"; exit 2; }
mkdir -p logs/raw
setsid nohup agy -p "$(cat "$P")" --model gemini-3.8-flash-high --output-format stream-json \
  --settings prompts/agy-settings-m1c.json > "logs/raw/agy-$N.jsonl" 2> "logs/raw/agy-$N.err" < /dev/null 9>&- &
PID=$!; disown
echo "dispatched agy run=$N pid=$PID log=logs/raw/agy-$N.jsonl model=gemini-3.8-flash-high"
