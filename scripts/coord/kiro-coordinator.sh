#!/usr/bin/env bash
# Persistent Kiro coordinator runner (run inside tmux session kiro-coord).
# Loops Kiro turns (resume same session) until DONE.md / BLOCKED.md / STOP exists, max 30 turns.
cd "$(dirname "$0")/../.."
K=$HOME/.local/bin/kiro-cli; C=docs/coordination; mkdir -p "$C" logs/raw
unset FACTORY_API_KEY FACTORY_TOKEN
SID=""; [ -f "$C/.session" ] && SID=$(cat "$C/.session")
for i in $(seq 1 30); do
  for f in DONE.md BLOCKED.md STOP; do [ -e "$C/$f" ] && { echo "$(date '+%F %T') stop: $f present"; exit 0; }; done
  TS=$(date +%Y%m%d-%H%M%S); OUT=logs/raw/kiro-coord-$TS.jsonl
  if [ -z "$SID" ]; then P=prompts/kiro-coordinator-start.md; R=(); else P=prompts/kiro-coordinator-continue.md; R=(--resume-id "$SID"); fi
  echo "$(date '+%F %T') turn $i session=${SID:-new} log=$OUT"
  $K chat --no-interactive --agent mesh-coordinator --model claude-opus-5.5 --effort high --output-format stream-json \
     "${R[@]}" "$(cat $P)" > "$OUT" 2> "${OUT%.jsonl}.err" < /dev/null
  rc=$?; echo "$(date '+%F %T') turn $i rc=$rc"
  if [ -z "$SID" ]; then SID=$(grep -o '"sessionId": *"[^"]*"' "$OUT" | head -1 | sed 's/.*"\([^"]*\)"$/\1/'); [ -n "$SID" ] && echo "$SID" > "$C/.session"; fi
  if grep -q CONTENT_FILTERED "$OUT" && ! grep -q '"tool_call"' "$OUT"; then echo "content filtered with no progress; starting a fresh session next turn"; SID=""; rm -f "$C/.session"; fi
  sleep 20
done
echo "$(date '+%F %T') max turns reached"
