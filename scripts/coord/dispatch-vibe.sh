#!/usr/bin/env bash
# Launch ONE detached Mistral Vibe implementation run (DeepSeek V4.1 Flash via Mistral; James decision 2026-11-08 10:23).
# Usage: bash scripts/coord/dispatch-vibe.sh prompts/<name>.md <run-name> [model-alias: ds-flash|glm-5-3]
# Permissions/allow-list live in ~/.vibe/config.toml (same read-only allow-list as prompts/agy-settings-m1c.json).
# No per-run price cap (James 11:08); --time-limit is the hang guard; cost is in the export dir. One coder at a time.
set -euo pipefail
SD="$(cd "$(dirname "$0")" && pwd)"
cd "$(dirname "$0")/../.."
P="${1:?prompt file}"; N="${2:?run name}"; M="${3:-ds-flash}"
[[ "$P" =~ ^prompts/[A-Za-z0-9._-]+\.md$ ]] || { echo "bad prompt path"; exit 2; }
[[ "$N" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "bad run name"; exit 2; }
[[ "$M" =~ ^(ds-flash|glm-5-3)$ ]] || { echo "model must be ds-flash or glm-5-3"; exit 2; }
[ -f "$P" ] || { echo "missing $P"; exit 2; }
source "$(dirname "$0")/coder-guard.sh"; coder_guard
[ -e "logs/raw/vibe-$N.jsonl" ] && { echo "REFUSED: run name exists"; exit 2; }
mkdir -p logs/raw
# Session budget (James 2026-11-09 18:49): short sessions; coder writes a handoff note and stops; fresh session continues.
G="prompts/.session-$N.md"; cat "$SD/session-budget.txt" "$P" > "$G"; P="$G"
setsid nohup "$HOME/.local/bin/vibe" --workdir "$PWD" --trust --add-dir /tmp --model "$M" --prompt-file "$P" --output streaming --output-dir "logs/raw/vibe-$N-export" \
  --time-limit 5400 --max-turns 200 > "logs/raw/vibe-$N.jsonl" 2> "logs/raw/vibe-$N.err" < /dev/null &
PID=$!; disown
echo "dispatched vibe run=$N pid=$PID model=$M log=logs/raw/vibe-$N.jsonl"
