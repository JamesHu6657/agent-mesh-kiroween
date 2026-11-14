#!/usr/bin/env bash
# Launch ONE detached Command Code implementation run (James 2026-11-08 11:17; subscription ~2 days, then back to vibe/DeepSeek).
# Usage: bash scripts/coord/dispatch-commandcode.sh prompts/<name>.md <run-name> [model]
# Permissions: --yolo, but the Command Code guard hook + deny rules (set up 11:17) mirror the read-only allow-list of agy-settings-m1c.json.
# Hang guard: timeout 3h. --max-turns 400. Usage/cost is in the final JSON result line of the log. One coder at a time.
set -euo pipefail
cd "$(dirname "$0")/../.."
P="${1:?prompt file}"; N="${2:?run name}"; M="${3:-deepseek/deepseek-v4.1-flash-ultrafast}"
[[ "$P" =~ ^prompts/[A-Za-z0-9._-]+\.md$ ]] || { echo "bad prompt path"; exit 2; }
[[ "$N" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "bad run name"; exit 2; }
[[ "$M" =~ ^deepseek/deepseek-v4\.1-flash(-ultrafast)?$ ]] || { echo "model must be deepseek-v4.1-flash[-ultrafast]"; exit 2; }
[ -f "$P" ] || { echo "missing $P"; exit 2; }
source "$(dirname "$0")/coder-guard.sh"; coder_guard
[ -e "logs/raw/cc-$N.jsonl" ] && { echo "REFUSED: run name exists"; exit 2; }
mkdir -p logs/raw
export PATH="$HOME/.npm-global/bin:$PATH"
setsid nohup timeout 10800 command-code -p "$(cat "$P")" --yolo --trust --skip-onboarding --no-auto-update --no-session \
  -m "$M" --max-turns 400 --output-format json > "logs/raw/cc-$N.jsonl" 2> "logs/raw/cc-$N.err" < /dev/null 9>&- &
PID=$!; disown
echo "dispatched commandcode run=$N pid=$PID model=$M log=logs/raw/cc-$N.jsonl"
