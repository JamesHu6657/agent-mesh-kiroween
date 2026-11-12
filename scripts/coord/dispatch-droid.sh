#!/usr/bin/env bash
# Launch ONE detached Factory Droid implementation run (James 2026-11-09 11:22: droid as strong coder, vibe is cheap backup).
# Usage (from inside the worktree): bash scripts/coord/dispatch-droid.sh prompts/<name>.md <run-name> [model]
# Safety: --auto medium (edits + local commands, no push/deploy) + scripts/coord/droid-settings.json command deny-list
# (push/remote/fetch, network tools, sudo, destructive git, ssh). Same per-worktree guard + max-2 cap as other coders.
# Hang guard: timeout 90 min (session-budget backstop; droid has no turn cap flag). Usage: final JSON result line has usage.factory_credits.
set -euo pipefail
# James 2026-11-10 08:26: droid paused for coding; Factory quota reserved for Fable 5.2 reviews.
[ -e "$HOME/ops/DROID_PAUSED" ] && { echo "REFUSED: droid paused (see ~/ops/DROID_PAUSED); use vibe or Quest coder"; exit 3; }
SD="$(cd "$(dirname "$0")" && pwd)"
P="${1:?prompt file}"; N="${2:?run name}"; M="${3:-claude-opus-5-5}"
[[ "$P" =~ ^prompts/[A-Za-z0-9._-]+\.md$ ]] || { echo "bad prompt path"; exit 2; }
[[ "$N" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "bad run name"; exit 2; }
[[ "$M" =~ ^(claude-opus-5-5|claude-opus-5|claude-fable-5\.1|gpt-6\.1-sol|claude-sonnet-5-5)$ ]] || { echo "model not allowed"; exit 2; }
[ -f "$P" ] || { echo "missing $P"; exit 2; }
case "$(pwd -P)" in "$HOME"/workspace/mesh-wt-*) ;; *) echo "REFUSED: run from a mesh-wt-* worktree"; exit 2;; esac
source "$(dirname "$0")/coder-guard.sh"; coder_guard
[ -e "logs/raw/droid-$N.jsonl" ] && { echo "REFUSED: run name exists"; exit 2; }
mkdir -p logs/raw
# Session budget (James 2026-11-09 18:49): short sessions; coder writes a handoff note and stops; fresh session continues.
G="prompts/.session-$N.md"; cat "$SD/session-budget.txt" "$P" > "$G"; P="$G"
S="$(cd "$(dirname "$0")" && pwd)/droid-settings.json"
setsid nohup timeout 5400 droid exec -f "$P" -m "$M" --auto medium --settings "$S" --cwd "$(pwd -P)" -o json \
  > "logs/raw/droid-$N.jsonl" 2> "logs/raw/droid-$N.err" < /dev/null &
PID=$!; disown
echo "dispatched droid run=$N pid=$PID model=$M log=logs/raw/droid-$N.jsonl"
