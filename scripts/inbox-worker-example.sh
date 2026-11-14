#!/usr/bin/env bash
# Inbox-worker example: wait for one task.offered message, start it, run YOUR command in the
# task workspace while a background loop keeps the lease alive, then submit (exit 0) or fail
# (non-zero exit).
#
#   MESH_TOKEN_FILE=~/.config/agent-mesh/worker1.token \
#     scripts/inbox-worker-example.sh -- ./do-the-work.sh
#
# The command runs with the task workspace as its current directory and these variables set:
#   MESH_TASK_ID  MESH_RUN_ID  MESH_FENCE  MESH_WORKSPACE  MESH_BRANCH
#   MESH_TASK_SPEC_FILE   a private temp file holding the task spec (outside the workspace)
# It should commit its work in the workspace (the task branch); this script does not commit,
# push or merge. See docs/inbox-workers.md for the protocol and the failure cases.
#
# Environment:
#   MESH_TOKEN_FILE  (required) agent token file, mode 0600, outside any workspace
#   MESH_BIN         mesh executable (default: mesh)
#   WAIT_TIMEOUT     how long one `inbox wait` blocks (default: 10m)
#   WAIT_ROUNDS      how many waits before giving up with exit 3 (default: 6; 0 = forever)
#   SUMMARY          submit summary (default: "completed by inbox-worker-example.sh")
#
# Exit codes: 0 submitted; 1 your command failed (task failed); 2 usage or setup error;
# 3 no task offered; 9 the lease was lost (nothing was submitted); other: mesh error.
# The token is only ever passed as --token-file; this script never reads or prints it.

set -u

MESH_BIN="${MESH_BIN:-mesh}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-10m}"
WAIT_ROUNDS="${WAIT_ROUNDS:-6}"
SUMMARY="${SUMMARY:-completed by inbox-worker-example.sh}"

die() { echo "inbox-worker-example: $*" >&2; exit 2; }

[ "${1:-}" = "--" ] && shift
[ "$#" -ge 1 ] || die "usage: MESH_TOKEN_FILE=FILE $0 -- COMMAND [ARGS...]"
[ -n "${MESH_TOKEN_FILE:-}" ] || die "set MESH_TOKEN_FILE to your agent token file"
[ -f "$MESH_TOKEN_FILE" ] || die "token file not found: $MESH_TOKEN_FILE"
mode="$(stat -c '%a' "$MESH_TOKEN_FILE" 2>/dev/null || stat -f '%Lp' "$MESH_TOKEN_FILE")"
[ "$mode" = "600" ] || die "token file must be mode 0600 (is $mode): chmod 600 FILE"
command -v python3 >/dev/null 2>&1 || die "python3 is required to read mesh's JSON output"
command -v "$MESH_BIN" >/dev/null 2>&1 || die "mesh executable not found: $MESH_BIN"

# Every mesh call goes through this function so the token is only passed as a file path.
mesh_cmd() { "$MESH_BIN" "$@" --token-file "$MESH_TOKEN_FILE"; }

# json_get FIELD...: read one mesh JSON response on stdin, print data.<FIELD> (empty if absent).
json_get() {
  python3 -c '
import json, sys
try:
    node = json.load(sys.stdin).get("data") or {}
except ValueError:
    sys.exit(0)
for key in sys.argv[1:]:
    node = node.get(key) if isinstance(node, dict) else None
print("" if node is None else node)
' "$@"
}

tmp="$(mktemp -d)" || die "cannot create a temp directory"
chmod 700 "$tmp"
hb_pid=""; cmd_pid=""
cleanup() {
  [ -n "$hb_pid" ] && kill "$hb_pid" 2>/dev/null
  [ -n "$cmd_pid" ] && kill -TERM -- "-$cmd_pid" 2>/dev/null
  rm -rf "$tmp"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# 1. Wait for a task.offered message. `inbox wait` returns at once if one is already pending
#    and exits 8 (WAIT_TIMEOUT, items empty) when nothing arrives in time.
msg_id=""; task_id=""; round=0
while [ -z "$task_id" ]; do
  round=$((round + 1))
  if [ "$WAIT_ROUNDS" -ne 0 ] && [ "$round" -gt "$WAIT_ROUNDS" ]; then
    echo "inbox-worker-example: no task offered after $WAIT_ROUNDS wait(s)" >&2
    exit 3
  fi
  mesh_cmd inbox wait --timeout "$WAIT_TIMEOUT" --max 10 -q > "$tmp/inbox.json" 2> "$tmp/inbox.err"
  wait_rc=$?
  case "$wait_rc" in
    0|8) ;;   # 8 = WAIT_TIMEOUT: nothing arrived, wait again
    10|11)    # daemon unavailable / busy: transient, retry after a pause
      echo "inbox-worker-example: inbox wait failed (exit $wait_rc), retrying in 5s" >&2
      sleep 5; continue ;;
    *)        # revoked token, forbidden, bad arguments...: not an empty inbox
      echo "inbox-worker-example: inbox wait failed (exit $wait_rc):" >&2
      cat "$tmp/inbox.json" "$tmp/inbox.err" >&2
      exit "$wait_rc" ;;
  esac
  found="$(python3 -c '
import json, sys
try:
    items = (json.load(open(sys.argv[1])).get("data") or {}).get("items") or []
except ValueError:
    items = []
offer = None
for item in items:
    if item.get("type") == "task.offered" and offer is None:
        offer = item
    elif item.get("id") and item.get("type") != "task.offered":
        # Further offers stay un-acked: a later wait returns them again (Linear full review D6).
        # Not an offer (rejection notice, project message...): show it and ack it below,
        # otherwise ten of them would fill every wait and hide later offers.
        print("other", item["id"], item.get("type", ""))
if offer is not None:
    print("offer", offer["id"], (offer.get("payload") or {}).get("task_id", ""))
' "$tmp/inbox.json")"
  while read -r kind mid rest; do
    [ -n "$kind" ] || continue
    if [ "$kind" = "other" ]; then
      echo "inbox-worker-example: acking $rest message $mid (not an offer; see mesh inbox list)" >&2
      mesh_cmd inbox ack "$mid" > /dev/null || echo "inbox-worker-example: could not ack $mid" >&2
    else
      msg_id="$mid"; task_id="$rest"
    fi
  done <<EOF
$found
EOF
done
echo "inbox-worker-example: offered $task_id" >&2

# 2. Acknowledge the offer (start does not do it; an un-acked offer is returned by every wait).
mesh_cmd inbox ack "$msg_id" > /dev/null || die "could not ack $msg_id"

# 3. Start. Every later write for this run must carry the exact run id, fence and session.
mesh_cmd task start "$task_id" > "$tmp/start.json"
start_rc=$?
if [ "$start_rc" -ne 0 ]; then
  echo "inbox-worker-example: task start failed (exit $start_rc):" >&2
  cat "$tmp/start.json" >&2
  exit "$start_rc"
fi
run_id="$(json_get run_id < "$tmp/start.json")"
fence="$(json_get fence < "$tmp/start.json")"
workspace="$(json_get workspace < "$tmp/start.json")"
branch="$(json_get branch < "$tmp/start.json")"
session="$(json_get session < "$tmp/start.json")"
every="$(json_get heartbeat_every_s < "$tmp/start.json")"
ttl="$(json_get lease_ttl_s < "$tmp/start.json")"
[ -n "$ttl" ] || ttl=120
[ -n "$run_id" ] && [ -n "$fence" ] && [ -d "$workspace" ] || die "unexpected task start response"
[ -n "$every" ] || every=40
[ -n "$session" ] && export MESH_SESSION="$session"

mesh_cmd task show "$task_id" | json_get spec > "$tmp/spec.txt"

# 4. Background heartbeat loop. If a heartbeat is refused (exit 9: lease expired, newer
#    fence, wrong session) the run is lost: record it and stop the command. Other errors
#    (daemon restarting, busy) are retried every few seconds; if no heartbeat succeeds for a
#    whole lease TTL the lease is gone anyway, so the run is treated as lost too.
lost="$tmp/lease-lost"
worker_pid=$$
(
  last_ok=$(date +%s); pause="$every"
  while sleep "$pause"; do
    mesh_cmd task heartbeat "$task_id" --run "$run_id" --fence "$fence" > /dev/null 2>&1
    rc=$?
    if [ "$rc" -eq 0 ]; then
      last_ok=$(date +%s); pause="$every"; continue
    fi
    if [ "$rc" -eq 9 ] || [ $(( $(date +%s) - last_ok )) -ge "$ttl" ]; then
      : > "$lost"; kill -TERM "$worker_pid" 2>/dev/null; exit 0
    fi
    echo "inbox-worker-example: heartbeat failed (exit $rc), retrying" >&2
    pause=$(( every < 5 ? every : 5 ))
  done
) &
hb_pid=$!

# 5. Run the command in the workspace, in its own process group, so a lost lease (or Ctrl-C)
#    stops the command AND everything it started, not just its first process.
export MESH_TASK_ID="$task_id" MESH_RUN_ID="$run_id" MESH_FENCE="$fence"
export MESH_WORKSPACE="$workspace" MESH_BRANCH="$branch" MESH_TASK_SPEC_FILE="$tmp/spec.txt"
( cd "$workspace" && exec python3 -c 'import os, sys; os.setpgid(0, 0); os.execvp(sys.argv[1], sys.argv[1:])' "$@" ) &
cmd_pid=$!
trap '[ -n "$cmd_pid" ] && kill -TERM -- "-$cmd_pid" 2>/dev/null' TERM
wait "$cmd_pid"; cmd_rc=$?
if [ -e "$lost" ]; then
  # A TERM from the heartbeat loop interrupts the first wait. Give the command group a bounded
  # grace, then SIGKILL it, so nothing keeps writing after the lease is gone (Linear full review D7).
  i=0
  while kill -0 -- "-$cmd_pid" 2>/dev/null && [ "$i" -lt "${KILL_GRACE_S:-10}" ]; do sleep 1; i=$((i + 1)); done
  kill -KILL -- "-$cmd_pid" 2>/dev/null
  wait "$cmd_pid" 2>/dev/null
fi
kill -TERM -- "-$cmd_pid" 2>/dev/null           # leftovers of the command's process group
trap 'exit 130' INT TERM
cmd_pid=""

kill "$hb_pid" 2>/dev/null; wait "$hb_pid" 2>/dev/null; hb_pid=""

# 6. Submit or fail. Skip both when the lease was lost: the writes would be refused.
if [ -e "$lost" ]; then
  echo "inbox-worker-example: lease lost; not submitting (see docs/inbox-workers.md)" >&2
  exit 9
fi
if [ "$cmd_rc" -eq 0 ]; then
  mesh_cmd task submit "$task_id" --run "$run_id" --fence "$fence" --summary "$SUMMARY" > /dev/null
  exit $?
fi
mesh_cmd task fail "$task_id" --run "$run_id" --fence "$fence" \
  --reason "command exited with status $cmd_rc" > /dev/null
echo "inbox-worker-example: command failed (exit $cmd_rc); task reported failed" >&2
exit 1
