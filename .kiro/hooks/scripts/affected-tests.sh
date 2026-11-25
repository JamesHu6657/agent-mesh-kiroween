#!/usr/bin/env bash
# affected-tests: run the unit test module(s) that match an edited Python file.
#
# Usage (from anywhere; the script cds to the repo root itself):
#   bash .kiro/hooks/scripts/affected-tests.sh <file> [<file> ...]
#   bash .kiro/hooks/scripts/affected-tests.sh
#       With no arguments it reads a kiro-cli hook event (JSON on stdin, field
#       tool_input.path) if one arrives within 2 s; otherwise it uses the changed
#       *.py files under mesh/ and tests/ from `git status --porcelain`.
#
# Mapping (unit tier only):
#   mesh/<...>/<name>.py                 -> tests/unit/test_<name>*.py
#   tests/unit/test_*.py                 -> that module
#   tests/<...>/<name>.py (not test_*)   -> tests/unit/test_<name>*.py
#   tests/{integration,recovery}/test_*  -> not run here (slow, daemon-based); use bash scripts/test.sh
#   tests/{probe,smoke}/*                -> never run here (opt-in tiers, plan §14.10)
#   __init__.py, __main__.py, other files -> nothing
#
# The modules run with PATH prefixed by tests/shims (as scripts/test.sh does) and a
# private tripwire file; a non-empty tripwire fails the run (checklist A3).
# Exit: rc of `python3 -m unittest`; 0 when nothing matched; 1 on a tripwire hit.

set -o pipefail

CALLER_PWD="$(pwd -P)" || exit 2
ROOT="$(cd "$(dirname "$0")/../../.." && pwd -P)" || exit 2
cd "$ROOT" || exit 2

files=()
from_git_status=0
if [ "$#" -gt 0 ]; then
  files=("$@")
else
  hook_path=""
  if [ ! -t 0 ]; then
    hook_path="$(python3 -c '
import json, os, select, sys, time
fd = sys.stdin.fileno()
buf = b""
end = time.monotonic() + 2.0
while True:
    left = end - time.monotonic()
    if left <= 0:
        break
    ready, _, _ = select.select([fd], [], [], left)
    if not ready:
        break
    chunk = os.read(fd, 65536)
    if not chunk:
        break
    buf += chunk
try:
    event = json.loads(buf.decode("utf-8", "replace")) if buf.strip() else {}
except ValueError:
    event = {}
tool_input = event.get("tool_input") if isinstance(event, dict) else None
path = ""
if isinstance(tool_input, dict):
    for key in ("path", "file_path"):
        if isinstance(tool_input.get(key), str):
            path = tool_input[key]
            break
print(path)
' 2>/dev/null)"
  fi
  if [ -n "$hook_path" ]; then
    files=("$hook_path")
  else
    from_git_status=1
    while IFS= read -r line; do
      p="${line:3}"
      case "$p" in *" -> "*) p="${p##* -> }" ;; esac
      files+=("$p")
    done < <(git status --porcelain --untracked-files=all -- mesh tests 2>/dev/null)
  fi
fi

modules=()
skipped=()

add_module() {
  local m="${1%.py}" x
  m="${m//\//.}"
  for x in "${modules[@]}"; do
    [ "$x" = "$m" ] && return 0
  done
  modules+=("$m")
}

map_name() {
  local t
  for t in tests/unit/test_"$1"*.py; do
    [ -f "$t" ] && add_module "$t"
  done
  case "$1" in
    terminate|children)
      [ -f tests/unit/test_termination_units.py ] && add_module tests/unit/test_termination_units.py
      ;;
    authz)
      for t in tests/unit/test_auth.py tests/unit/test_projects.py; do
        [ -f "$t" ] && add_module "$t"
      done
      ;;
    task)
      for t in tests/unit/test_leases.py tests/unit/test_projects.py tests/unit/test_friction4.py tests/unit/test_friction5.py; do
        [ -f "$t" ] && add_module "$t"
      done
      ;;
    approvals|approval)
      for t in tests/unit/test_projects.py tests/unit/test_friction4.py; do
        [ -f "$t" ] && add_module "$t"
      done
      ;;
    agy|droid|kiro|vibe|base|registry|smoke_schema)
      for t in tests/unit/test_adapters*.py; do
        [ -f "$t" ] && add_module "$t"
      done
      ;;
  esac
}

normalize() {
  local f="$1" r cand
  case "$f" in
    /*)
      r="$(realpath -m -- "$f" 2>/dev/null || printf '%s' "$f")"
      ;;
    *)
      if [ "$from_git_status" -eq 1 ]; then
        cand="$ROOT/$f"
      elif [ -e "$CALLER_PWD/$f" ]; then
        cand="$CALLER_PWD/$f"
      elif [ -e "$ROOT/$f" ]; then
        cand="$ROOT/$f"
      else
        cand="$CALLER_PWD/$f"
      fi
      r="$(realpath -m -- "$cand" 2>/dev/null || printf '%s' "$cand")"
      ;;
  esac
  case "$r" in
    "$ROOT"/*) printf '%s' "${r#"$ROOT"/}" ;;
    *) printf '' ;;
  esac
}

for raw in "${files[@]}"; do
  f="$(normalize "$raw")"
  case "$f" in *.py) ;; *) continue ;; esac
  name="$(basename -- "$f" .py)"
  case "$name" in __init__|__main__) continue ;; esac
  case "$f" in
    tests/unit/test_*.py)
      [ -f "$f" ] && add_module "$f"
      ;;
    tests/probe/*|tests/smoke/*)
      skipped+=("$f (opt-in tier)")
      ;;
    tests/integration/test_*|tests/recovery/test_*)
      skipped+=("$f (integration/recovery: run bash scripts/test.sh or python3 -m unittest $f)")
      ;;
    mesh/*|tests/*)
      map_name "$name"
      ;;
  esac
done

for s in "${skipped[@]}"; do
  echo "affected-tests: skipped $s"
done

if [ "${#modules[@]}" -eq 0 ]; then
  echo "affected-tests: no matching unit test module for: ${files[*]:-<no changed files>}"
  exit 0
fi

TW="$(mktemp /tmp/mesh-tripwire-affected-XXXXXX)" || exit 2
trap 'rm -f "$TW"' EXIT

echo "affected-tests: python3 -m unittest ${modules[*]}"
PATH="$ROOT/tests/shims:$ROOT/bin:$PATH" \
_TRIPWIRE_FILE="$TW" _SUITE_TRIPWIRE_FILE="$TW" \
  python3 -m unittest "${modules[@]}"
rc=$?

if [ -s "$TW" ]; then
  echo "affected-tests: FAIL: tripwire hit (a vendor CLI shim was called):"
  cat "$TW"
  exit 1
fi
echo "affected-tests: rc $rc"
exit "$rc"
