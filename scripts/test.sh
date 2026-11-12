#!/bin/sh
set -e

# Agent Mesh test runner (plan §14.10)
# Runs unit, integration, and recovery tests offline with tripwire shims.

REPO_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)"
cd "$REPO_ROOT"

START_TIME=$(date +%s)

if [ "$1" = "--check" ]; then
  echo "scripts/test.sh: syntax and runner OK"
  exit 0
fi

# Ensure isolated tripwire log path
# Keyed by repo root so concurrent suites in other worktrees cannot clear or trip
# each other's tripwire (review m1c-run3 #7).
REPO_KEY="$(printf '%s' "$REPO_ROOT" | cksum | cut -d' ' -f1)"
TRIPWIRE_FILE="/tmp/mesh-tripwire-suite-$REPO_KEY.log"
TRIPWIRE_DEFAULT="/tmp/mesh-tripwire-hit.log"
python3 -c "import os; [os.remove(p) for p in ('$TRIPWIRE_FILE', '$TRIPWIRE_DEFAULT') if os.path.exists(p)]"
export MESH_TRIPWIRE_FILE="$TRIPWIRE_FILE"
export MESH_SUITE_TRIPWIRE_FILE="$TRIPWIRE_FILE"

# Register suite ownership before cleanup, protecting this and concurrent suites.
MESH_TEST_SUITE_ID="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
MESH_TEST_DAEMON_REGISTRY="${MESH_TEST_DAEMON_REGISTRY:-/tmp/mesh-test-daemons}"
export MESH_TEST_DAEMON_REGISTRY
SUITE_LOCK_HELD=0
SUITE_LOCK="$MESH_TEST_DAEMON_REGISTRY/suite-$MESH_TEST_SUITE_ID.lock"
if python3 scripts/coord/reap_daemons.py --prepare-registry &&
   (umask 077; : > "$SUITE_LOCK"); then
  exec 9>"$SUITE_LOCK"
  if flock -n 9; then
    SUITE_LOCK_HELD=1
    export MESH_TEST_SUITE_ID
  else
    exec 9>&-
  fi
fi
if [ "$SUITE_LOCK_HELD" -eq 0 ]; then
  echo "reap-daemons: suite lock unavailable; using adhoc process ownership" >&2
  unset MESH_TEST_SUITE_ID
fi

# BEGIN SUITE CLEANUP (also exercised directly by the isolated unit tests)
cleanup_suite() {
  suite_status=$?
  trap - EXIT
  if [ "$SUITE_LOCK_HELD" -eq 1 ]; then
    flock -u 9 || true
    exec 9>&-
  fi
  timeout 30 python3 "$REPO_ROOT/scripts/coord/reap_daemons.py" || true
  if [ "$SUITE_LOCK_HELD" -eq 1 ]; then
    rm -f "$SUITE_LOCK" || true
  fi
  exit "$suite_status"
}
trap cleanup_suite EXIT
# dash (#!/bin/sh) skips EXIT traps on fatal signals; turn them into exits.
# POSIX: a signal ignored on entry to a non-interactive shell cannot be trapped
# or reset, so when launched as e.g. `nohup scripts/test.sh &` (INT/HUP ignored)
# the INT/HUP traps below are no-ops; TERM still works. To get Ctrl-C cleanup,
# launch with default dispositions (foreground, or a tmux pane); `trap - INT`
# inside a backgrounded shell cannot undo an inherited ignore.
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
# END SUITE CLEANUP

timeout 30 python3 scripts/coord/reap_daemons.py || true

ORIG_PATH="$PATH"
export PATH="$REPO_ROOT/tests/shims:$REPO_ROOT/bin:$PATH"

# 1. Run unit test tier (pure offline, no real CLIs)
echo "=== Running Unit Tests ==="
python3 -m unittest discover -s tests/unit -t .

# 2. Run integration tier with PATH prefixed with tripwire shims
# (via run_tier.py; replaces discover -s tests/integration)
echo "=== Running Integration Tests ==="
python3 tests/run_tier.py tests/integration

# 3. Run recovery tier
# (via run_tier.py; replaces discover -s tests/recovery)
echo "=== Running Recovery Tests ==="
python3 tests/run_tier.py tests/recovery
export PATH="$ORIG_PATH"

# 4. Verify tripwire files are empty or absent (ACC: A3)
if [ -f "$TRIPWIRE_FILE" ] && [ -s "$TRIPWIRE_FILE" ]; then
  echo "FAIL: Suite tripwire was triggered during test suite run!"
  cat "$TRIPWIRE_FILE"
  exit 1
fi

if [ -f "$TRIPWIRE_DEFAULT" ] && [ -s "$TRIPWIRE_DEFAULT" ]; then
  echo "FAIL: Default tripwire was triggered during test suite run!"
  cat "$TRIPWIRE_DEFAULT"
  exit 1
fi

END_TIME=$(date +%s)
TOTAL_WALL_TIME=$((END_TIME - START_TIME))

echo "=== All test tiers passed successfully ==="
echo "Total wall time: ${TOTAL_WALL_TIME}s"
exit 0
