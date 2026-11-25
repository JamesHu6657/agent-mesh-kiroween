#!/usr/bin/env bash
# Read-only status snapshot for the coordinator.
cd "$(dirname "$0")/../.."
echo "== agy runs"; pgrep -af '^agy -p' | cut -c1-80 || echo none
echo "== mesh daemons"; pgrep -af ' -m mesh .*daemon run' || echo none
echo "== git"; git log --format='%h %an %s' -8; git status --short | head -30
echo "== latest agy logs"; ls -t logs/raw/agy-*.jsonl 2>/dev/null | head -3
