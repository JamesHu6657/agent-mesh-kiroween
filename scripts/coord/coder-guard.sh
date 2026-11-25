# Sourced by dispatch-*.sh. Per-worktree one-coder guard + global cap (James 2026-11-08 13:29: max 2 concurrent coders,
# one per git worktree). A coder "belongs" to the worktree that is its cwd; only Mesh repo/worktrees count.
# One coder run can be several processes (timeout wrapper + CLI), so the cap counts distinct worktrees.
coder_pids() { pgrep -f '^agy -p|(^|[ /])vibe(-rs)? .*--prompt-file|command-code.* -p |droid exec -f ' || true; }
# Linear full review D1: check-then-launch is serialized by a lock (fd 9) held from the scan
# until this dispatch's coder is visible (or 20 s), so two dispatches cannot both pass the scan.
# Dispatch scripts close fd 9 for the coder (9>&-), so the coder never holds the lock.
# No time limit (PR #39 review): the lock is kept until the coder is visible or every job this
# dispatch launched has exited (a failed start), so a slow start never lets a second one in.
_coder_guard_in_git_tree() {
  local d="$1"
  while :; do
    [ -e "$d/.git" ] && return 0
    [ "$d" = "/" ] || [ -z "$d" ] && return 1
    d="${d%/*}"
    [ -n "$d" ] || d="/"
  done
}
_coder_guard_wt_root() {
  local dir="$1" wts="$2" best="" wt rel
  if [ -n "$wts" ]; then
    while IFS= read -r wt; do
      [ -n "$wt" ] || continue
      case "$dir" in
        "$wt"|"${wt%/}"/*)
          if [ "${#wt}" -gt "${#best}" ]; then best="$wt"; fi
          ;;
      esac
    done <<EOF
$wts
EOF
  fi
  if [ -n "$best" ]; then
    printf '%s\n' "$best"
    return 0
  fi
  case "$dir" in
    "$HOME"/workspace/mesh-*)
      rel="${dir#"$HOME/workspace/"}"
      printf '%s\n' "$HOME/workspace/${rel%%/*}"
      return 0
      ;;
  esac
  printf '%s\n' "$dir"
}
coder_guard_release() {
  local p cwd cwd_root j alive
  while :; do
    for p in $(coder_pids); do
      cwd="$(readlink -f /proc/$p/cwd 2>/dev/null || true)"
      [ -n "$cwd" ] || continue
      if [ "$cwd" = "$CODER_GUARD_HERE" ]; then exec 9>&-; return 0; fi
      if [ -n "${CODER_GUARD_HERE_ROOT:-}" ]; then
        cwd_root="$(_coder_guard_wt_root "$cwd" "${CODER_GUARD_WORKTREES:-}")"
        [ "$cwd_root" = "$CODER_GUARD_HERE_ROOT" ] && { exec 9>&-; return 0; }
      fi
    done
    alive=""
    # $! survives `disown` (the dispatchers disown their launcher), the job table does not.
    for j in $(jobs -p) ${!:-}; do kill -0 "$j" 2>/dev/null && alive=1; done
    [ -n "$alive" ] || { exec 9>&-; return 0; }
    sleep 0.5
  done
}
coder_guard() {
  local here here_root p cwd cwd_root busy="" busy_count=0 busy_list="" worktrees="" wt matched wt_out wt_rc=0 line
  here="$(pwd -P)"
  wt_out="$(LC_ALL=C git worktree list --porcelain 2>&1)" || wt_rc=$?
  if [ "$wt_rc" -eq 0 ]; then
    while IFS= read -r line; do
      case "$line" in
        "worktree "*)
          wt="${line#worktree }"
          wt="$(readlink -f "$wt" 2>/dev/null || printf '%s\n' "$wt")"
          [ -n "$wt" ] && worktrees="${worktrees:+$worktrees$'\n'}$wt"
          ;;
      esac
    done <<EOF
$wt_out
EOF
  elif [ -z "${GIT_DIR:-}" ] && [ -z "${GIT_WORK_TREE:-}" ] && ! _coder_guard_in_git_tree "$here" && [[ "$wt_out" == *"not a git repository"* ]]; then
    worktrees=""
  else
    echo "REFUSED: git worktree list failed (${wt_out:-exit $wt_rc})"
    exit 3
  fi
  here_root="$(_coder_guard_wt_root "$here" "$worktrees")"
  exec 9>"${CODER_GUARD_LOCK:-/tmp/mesh-coder-guard.lock}"
  flock -w 120 9 || { echo "REFUSED: coder admission lock busy"; exit 3; }
  CODER_GUARD_HERE="$here"
  CODER_GUARD_HERE_ROOT="$here_root"
  CODER_GUARD_WORKTREES="$worktrees"
  for p in $(coder_pids); do
    cwd="$(readlink -f /proc/$p/cwd 2>/dev/null || true)"; [ -n "$cwd" ] || continue
    matched=""
    case "$cwd" in "$HOME"/workspace/mesh-*) matched=1 ;; esac
    if [ -z "$matched" ] && [ "$cwd" = "$here" ]; then matched=1; fi
    if [ -z "$matched" ] && [ -n "$worktrees" ]; then
      while IFS= read -r wt; do
        [ -n "$wt" ] || continue
        case "$cwd" in "$wt"|"${wt%/}"/*) matched=1; break ;; esac
      done <<EOF
$worktrees
EOF
    fi
    [ -n "$matched" ] || continue
    cwd_root="$(_coder_guard_wt_root "$cwd" "$worktrees")"
    if [ "$cwd_root" = "$here_root" ]; then echo "REFUSED: a coder run is already active in this worktree (pid $p)"; exit 3; fi
    case $'\n'"$busy"$'\n' in *$'\n'"$cwd_root"$'\n'*) ;; *) busy="${busy:+$busy$'\n'}$cwd_root" ;; esac
  done
  if [ -n "$busy" ]; then
    while IFS= read -r wt; do
      [ -n "$wt" ] || continue
      busy_count=$((busy_count + 1))
      busy_list="$busy_list $wt"
    done <<EOF
$busy
EOF
  fi
  if [ "$busy_count" -ge "${MAX_CODERS:-2}" ]; then echo "REFUSED: $busy_count coder worktrees active (max ${MAX_CODERS:-2}):$busy_list"; exit 3; fi
  trap coder_guard_release EXIT  # admitted: keep the lock until this coder is visible
}
