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
mesh_cmd() {
  if [ "${_MESH_EXEC:-0}" = 1 ]; then
    exec "$MESH_BIN" "$@" --token-file "$MESH_TOKEN_FILE"
  fi
  "$MESH_BIN" "$@" --token-file "$MESH_TOKEN_FILE"
}

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
hb_pid=""; cmd_pid=""; reaped=0; pgid_gone=0; cmd_rc=0; _jn=0

# Liveness of thread-group $1 from /proc (matches mesh/core/proc.py:is_tgid_alive).
# Returns 0 alive, 1 gone, 2 unknown (entry present but unreadable: EACCES/EIO are never "dead").
# On Linux, a leader whose main thread called pthread_exit reports state Z while num_threads > 1.
_proc_tgid_alive() {
  local pid="$1" l td tl tst
  if ! IFS= read -r l 2>/dev/null < "/proc/$pid/stat"; then
    [ -e "/proc/$pid" ] && return 2
    return 1
  fi
  l="${l##*) }"
  set -f; set -- $l; set +f
  case "${1:-}" in
    "") return 2 ;;
    Z|X|x)
      [ "${18:-1}" -gt 1 ] 2>/dev/null && return 0
      if [ -r "/proc/$pid/status" ]; then
        while IFS= read -r l; do
          case "$l" in
            Threads:*)
              l="${l#Threads:}"
              set -f; set -- $l; set +f
              [ "${1:-1}" -gt 1 ] 2>/dev/null && return 0
              break
              ;;
          esac
        done 2>/dev/null < "/proc/$pid/status"
      fi
      if [ -e "/proc/$pid/task" ] && { [ ! -r "/proc/$pid/task" ] || [ ! -x "/proc/$pid/task" ]; }; then
        return 2
      fi
      for td in "/proc/$pid/task"/[0-9]*; do
        [ -e "$td" ] || continue
        IFS= read -r tl 2>/dev/null < "$td/stat" || { [ -e "$td" ] && return 2; continue; }
        tl="${tl##*) }"
        tst="${tl%% *}"
        case "$tst" in ""|Z|X|x) ;; *) return 0 ;; esac
      done
      return 1
      ;;
    *) return 0 ;;
  esac
}

# Process-group scan via ps for systems without (readable) /proc. POSIX `ps -A` selects
# every process (BSD `ps -e` only adds environment output); OpenBSD falls back to `ps -ax`.
# Each -o column is passed separately per POSIX `ps(1p)` (`-o a=,b=` treats `,b=` as a's header).
# Returns 0 live, 1 empty, 2 ps unusable.
_pgid_has_live_ps() {
  local pgid="$1" l pg nlwp st saw_row
  command -v ps >/dev/null 2>&1 || return 2
  if l="$(ps -A -o pgid= -o nlwp= -o stat= 2>/dev/null)" && [ -n "$l" ]; then
    saw_row=0
    while read -r pg nlwp st _; do
      case "$pg" in *[!0-9]*|"") continue ;; *) saw_row=1 ;; esac
      [ "$pg" = "$pgid" ] || continue
      case "$st" in
        "") return 2 ;;
        Z*|X*|x*) [ "${nlwp:-1}" -gt 1 ] 2>/dev/null && return 0 ;;
        *) return 0 ;;
      esac
    done <<PSLIST
$l
PSLIST
    [ "$saw_row" -eq 1 ] && return 1
    return 2
  fi
  if { l="$(ps -A -o pgid= -o stat= 2>/dev/null)" && [ -n "$l" ]; } ||
     { l="$(ps -ax -o pgid= -o stat= 2>/dev/null)" && [ -n "$l" ]; }; then
    saw_row=0
    while read -r pg st _; do
      case "$pg" in *[!0-9]*|"") continue ;; *) saw_row=1 ;; esac
      [ "$pg" = "$pgid" ] || continue
      case "$st" in
        "") return 2 ;;
        Z*|X*|x*) ;;
        *) return 0 ;;
      esac
    done <<PSLIST
$l
PSLIST
    [ "$saw_row" -eq 1 ] && return 1
  fi
  return 2
}

# True if PGID $1 has any live (non-zombie or multithreaded leader-zombie) process.
# kill -0 -- -pgid returns 0 even when only <defunct> (Z) zombies remain under a non-reaping PID 1.
# When /proc is readable and any entry is unreadable, fail closed (return 0) without deferring
# to ps (which reads the same /proc and silently skips EACCES entries).
_pgid_has_live() {
  local pgid="$1" d pid l unreadable=0 rc
  kill -0 -- "-$pgid" 2>/dev/null || return 1
  if [ -r "/proc/$$/stat" ]; then
    for d in /proc/[0-9]*; do
      if ! IFS= read -r l 2>/dev/null < "$d/stat"; then
        [ -d "$d" ] && unreadable=1
        continue
      fi
      pid="${d##*/}"
      l="${l##*) }"
      set -f; set -- $l; set +f
      [ "${3:-}" = "$pgid" ] || continue
      case "${1:-}" in
        "") unreadable=1 ;;
        Z|X|x)
          _proc_tgid_alive "$pid"; rc=$?
          [ "$rc" -eq 0 ] && return 0
          [ "$rc" -eq 2 ] && unreadable=1
          ;;
        *) return 0 ;;
      esac
    done
    [ "$unreadable" -eq 0 ] && return 1
    return 0
  fi
  _pgid_has_live_ps "$pgid"; rc=$?
  [ "$rc" -eq 1 ] && return 1
  return 0
}

# True while $cmd_pid is still an un-reaped, running job of THIS shell. Bash reaps a
# finished background child on SIGCHLD and the kernel may then hand its PID to an
# unrelated process, so once the job has left `jobs -pr` the PID is never probed or
# signalled again (review PR47 R4). A pthread_exit leader-zombie with live threads is
# not waitable yet, so it correctly stays a running job. The list goes through a
# per-call file (no subshell, and a trap firing mid-loop cannot clobber it).
_cmd_running() {
  local j f rc=1
  [ -n "$cmd_pid" ] && [ "$reaped" -eq 0 ] || return 1
  _jn=$((_jn + 1)); f="$tmp/jobs.$_jn"
  if ! jobs -pr > "$f" 2>/dev/null; then
    rm -f "$f"
    return 0
  fi
  while IFS= read -r j; do
    [ "$j" = "$cmd_pid" ] && { rc=0; break; }
  done < "$f"
  rm -f "$f"
  return "$rc"
}

# Collect the exit status of a job that has left the running list. `wait` on a finished
# job returns at once (from bash's saved status), so this never blocks or spins.
_reap_cmd() {
  [ "$reaped" -eq 0 ] || return 0
  wait "$cmd_pid" 2>/dev/null; cmd_rc=$?
  reaped=1
}

# Signal command/PGID safely: the positive cmd_pid only while it is still our running
# job, the group only while it has live members; the group is marked gone only once
# the leader itself has been reaped.
_signal_cmd() {
  local sig="$1"
  [ -n "$cmd_pid" ] || return 0
  [ "$pgid_gone" -eq 0 ] || return 0
  if _cmd_running; then
    kill -"$sig" -- "$cmd_pid" "-$cmd_pid" 2>/dev/null
    return 0
  fi
  if _pgid_has_live "$cmd_pid"; then
    kill -"$sig" -- "-$cmd_pid" 2>/dev/null
  elif [ "$reaped" -eq 1 ]; then
    pgid_gone=1
  fi
}

cleanup() {
  [ -n "$hb_pid" ] && { kill "$hb_pid" 2>/dev/null; wait "$hb_pid" 2>/dev/null; }
  _signal_cmd TERM
  rm -rf "$tmp"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# 1. Wait for a task.offered message. `inbox wait` returns at once if one is already pending
#    and exits 8 (WAIT_TIMEOUT, items empty) when nothing arrives in time.
msg_id=""; task_id=""; round=0; last_wait_rc=0
while [ -z "$task_id" ]; do
  round=$((round + 1))
  if [ "$WAIT_ROUNDS" -ne 0 ] && [ "$round" -gt "$WAIT_ROUNDS" ]; then
    if [ "$last_wait_rc" -eq 10 ] || [ "$last_wait_rc" -eq 11 ]; then
      echo "inbox-worker-example: inbox wait failed (exit $last_wait_rc):" >&2
      cat "$tmp/inbox.json" "$tmp/inbox.err" >&2
      exit "$last_wait_rc"
    fi
    echo "inbox-worker-example: no task offered after $WAIT_ROUNDS wait(s)" >&2
    exit 3
  fi
  mesh_cmd inbox wait --timeout "$WAIT_TIMEOUT" --max 10 -q > "$tmp/inbox.json" 2> "$tmp/inbox.err"
  wait_rc=$?
  last_wait_rc="$wait_rc"
  case "$wait_rc" in
    0|8) ;;   # 8 = WAIT_TIMEOUT: nothing arrived, wait again
    10|11)    # daemon unavailable / busy: transient, retry after a pause
      if [ "$WAIT_ROUNDS" -ne 0 ] && [ "$round" -ge "$WAIT_ROUNDS" ]; then
        echo "inbox-worker-example: inbox wait failed (exit $wait_rc):" >&2
        cat "$tmp/inbox.json" "$tmp/inbox.err" >&2
        exit "$wait_rc"
      fi
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
  acked_other=0
  while read -r kind mid rest; do
    [ -n "$kind" ] || continue
    if [ "$kind" = "other" ]; then
      echo "inbox-worker-example: acking $rest message $mid (not an offer; see mesh inbox list)" >&2
      if mesh_cmd inbox ack "$mid" > /dev/null; then
        acked_other=1
      else
        echo "inbox-worker-example: could not ack $mid" >&2
      fi
    else
      msg_id="$mid"; task_id="$rest"
    fi
  done <<EOF
$found
EOF
  if [ -z "$task_id" ] && [ "$acked_other" -eq 1 ]; then
    round=$((round - 1))
  fi
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
# Full review P1: install USR1/INT/TERM traps before starting hb_pid so an early USR1
# cannot fatally terminate the worker without cleanup; wake/stop both PID and PGID.
stopping=""
trap '_signal_cmd TERM' USR1
trap 'stopping=1; _signal_cmd TERM' INT TERM
(
  exec </dev/null >/dev/null
  hb_child=""
  hb_cleanup() {
    trap - TERM INT EXIT
    [ -n "$hb_child" ] && { kill "$hb_child" 2>/dev/null; wait "$hb_child" 2>/dev/null; }
    exit 0
  }
  trap hb_cleanup TERM INT EXIT
  last_ok=$(date +%s); pause="$every"
  while :; do
    sleep "$pause" </dev/null >/dev/null 2>&1 &
    hb_child=$!
    wait "$hb_child" || break
    hb_child=""
    _MESH_EXEC=1 mesh_cmd task heartbeat "$task_id" --run "$run_id" --fence "$fence" </dev/null >/dev/null 2>&1 &
    hb_child=$!
    wait "$hb_child"
    rc=$?
    hb_child=""
    if [ "$rc" -eq 0 ]; then
      last_ok=$(date +%s); pause="$every"; continue
    fi
    if [ "$rc" -eq 9 ] || [ $(( $(date +%s) - last_ok )) -ge "$ttl" ]; then
      : > "$lost"; kill -USR1 "$worker_pid" 2>/dev/null; exit 0
    fi
    echo "inbox-worker-example: heartbeat failed (exit $rc), retrying" >&2
    pause=$(( every < 5 ? every : 5 ))
  done
) &
hb_pid=$!

# 5. Run the command in the workspace, in its own process group, so a lost lease (or Ctrl-C)
#    stops the command AND everything it started, not just its first process.
#    Strip credential/session variables before execing the child command.
export MESH_TASK_ID="$task_id" MESH_RUN_ID="$run_id" MESH_FENCE="$fence"
export MESH_WORKSPACE="$workspace" MESH_BRANCH="$branch" MESH_TASK_SPEC_FILE="$tmp/spec.txt"
cmd_rc=0
if [ ! -e "$lost" ] && [ -z "$stopping" ]; then
  reaped=0; pgid_gone=0
  pgid_ready="$tmp/pgid.ready"
  (
    trap ': > "$pgid_ready"' EXIT
    cd "$workspace" || exit 1
    exec python3 -c 'import os, sys
for k in ("MESH_TOKEN_FILE", "MESH_SESSION", "MESH_" + "TOKEN"):
    os.environ.pop(k, None)
os.setpgid(0, 0)
open(sys.argv[1], "w").close()
os.execvp(sys.argv[2], sys.argv[2:])' "$pgid_ready" "$@"
  ) &
  cmd_pid=$!
  while [ ! -e "$pgid_ready" ] && [ ! -e "$lost" ] && [ -z "$stopping" ]; do
    _cmd_running || break
    sleep 0.02 2>/dev/null || sleep 1
  done
  while [ ! -e "$lost" ] && [ -z "$stopping" ]; do
    if ! _cmd_running; then
      _reap_cmd
      _pgid_has_live "$cmd_pid" || pgid_gone=1
      break
    fi
    sleep 0.1 2>/dev/null || sleep 1
  done
  if [ "$reaped" -eq 0 ] || { [ "$pgid_gone" -eq 0 ] && _pgid_has_live "$cmd_pid"; }; then
    _signal_cmd TERM
    # Give the command group a bounded grace, then SIGKILL it, so nothing keeps writing
    # after the lease is gone (Linear full review D7).
    i=0
    steps=$(( ${KILL_GRACE_S:-10} * 5 ))
    while [ "$i" -lt "$steps" ]; do
      if [ "$reaped" -eq 0 ] && ! _cmd_running; then
        _reap_cmd
      fi
      if [ "$reaped" -eq 1 ]; then
        if [ "$pgid_gone" -eq 1 ] || ! _pgid_has_live "$cmd_pid"; then
          pgid_gone=1
          break
        fi
      fi
      sleep 0.2 2>/dev/null || sleep 1
      i=$((i + 1))
    done
    if [ "$reaped" -eq 0 ] || [ "$pgid_gone" -eq 0 ]; then
      if [ "$reaped" -eq 0 ] && ! _cmd_running; then
        _reap_cmd
      fi
      if [ "$reaped" -eq 0 ]; then
        _signal_cmd KILL
        n=0
        while _cmd_running; do
          sleep 0.05 2>/dev/null || sleep 1
          n=$((n + 1))
          [ $((n % 20)) -eq 0 ] && _signal_cmd KILL
        done
        _reap_cmd
      fi
      if _pgid_has_live "$cmd_pid"; then
        kill -KILL -- "-$cmd_pid" 2>/dev/null
        n=0
        while [ "$n" -lt 10 ] && _pgid_has_live "$cmd_pid"; do
          sleep 0.05 2>/dev/null || { sleep 1; break; }
          n=$((n + 1))
          [ "$n" -eq 5 ] && kill -KILL -- "-$cmd_pid" 2>/dev/null
        done
      fi
      pgid_gone=1
    fi
  fi
fi
pgid_gone=1
cmd_pid=""
trap 'exit 130' INT TERM

kill "$hb_pid" 2>/dev/null; wait "$hb_pid" 2>/dev/null; hb_pid=""
trap - USR1
if [ -n "$stopping" ]; then
  echo "inbox-worker-example: stopped by signal; task left running for its lease to expire" >&2
  exit 143
fi

# 6. Submit or fail. Skip both when the lease was lost: the writes would be refused.
if [ -e "$lost" ]; then
  echo "inbox-worker-example: lease lost; not submitting (see docs/inbox-workers.md)" >&2
  exit 9
fi
if [ "$cmd_rc" -eq 0 ]; then
  mesh_cmd task submit "$task_id" --run "$run_id" --fence "$fence" --summary="$SUMMARY" > /dev/null
  exit $?
fi
mesh_cmd task fail "$task_id" --run "$run_id" --fence "$fence" \
  --reason="command exited with status $cmd_rc" > /dev/null
echo "inbox-worker-example: command failed (exit $cmd_rc); task reported failed" >&2
exit 1
