#!/usr/bin/env bash
# Bounded wait for a PID (max 600 s). Usage: bash scripts/coord/wait-pid.sh <pid> <seconds>
set -u; PID="${1:?pid}"; S="${2:-600}"; [[ "$PID" =~ ^[0-9]+$ && "$S" =~ ^[0-9]+$ ]] || exit 2; [ "$S" -gt 600 ] && S=600
timeout "$S" tail --pid="$PID" -f /dev/null; if kill -0 "$PID" 2>/dev/null; then echo "still running pid=$PID"; else echo "exited pid=$PID"; fi
