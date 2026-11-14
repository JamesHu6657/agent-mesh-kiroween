#!/usr/bin/env bash
# Coordinator-only worktree management for parallel coder runs (James 2026-11-08 13:29).
# Usage: bash scripts/coord/worktree.sh add <name> [base-ref] | rm <name> | list
# Worktree: ~/workspace/mesh-wt-<name>, branch wt/<name>. Coders run inside it; merges to main are done by Quest, one at a time.
set -euo pipefail
cd "$(dirname "$0")/../.."
cmd="${1:?add|rm|list}"; name="${2:-}"
case "$cmd" in
  add) [[ "$name" =~ ^[a-z0-9-]+$ ]] || { echo "bad name"; exit 2; }
       base="${3:-main}"; dir="$HOME/workspace/mesh-wt-$name"
       git worktree add -b "wt/$name" "$dir" "$base"; mkdir -p "$dir/logs/raw"; echo "worktree $dir on wt/$name from $base";;
  rm)  [[ "$name" =~ ^[a-z0-9-]+$ ]] || { echo "bad name"; exit 2; }
       dir="$HOME/workspace/mesh-wt-$name"
       [ -z "$(git -C "$dir" status --porcelain)" ] || { echo "REFUSED: $dir has uncommitted changes"; exit 3; }
       mkdir -p "logs/raw/wt-$name"; cp -r "$dir/logs/raw/." "logs/raw/wt-$name/" 2>/dev/null || true
       git worktree remove "$dir"; echo "removed $dir (branch wt/$name kept until merged)";;
  list) git worktree list;;
  *) echo "usage"; exit 2;;
esac
