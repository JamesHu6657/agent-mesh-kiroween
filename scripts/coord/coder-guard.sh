# Sourced by dispatch-*.sh. Per-worktree one-coder guard + global cap (James 2026-11-08 13:29: max 2 concurrent coders,
# one per git worktree). A coder "belongs" to the worktree that is its cwd; only Mesh repo/worktrees count.
# One coder run can be several processes (timeout wrapper + CLI), so the cap counts distinct worktrees.
coder_pids() { pgrep -f '^agy -p|vibe-rs.*--prompt-file|command-code.* -p |droid exec -f ' || true; }
# Linear full review D1: check-then-launch is serialized by a lock (fd 9) held from the scan
# until this dispatch's coder is visible (or 20 s), so two dispatches cannot both pass the scan.
# Dispatch scripts close fd 9 for the coder (9>&-), so the coder never holds the lock.
# No time limit (PR #39 review): the lock is kept until the coder is visible or every job this
# dispatch launched has exited (a failed start), so a slow start never lets a second one in.
coder_guard_release() {
  local p cwd j alive
  while :; do
    for p in $(coder_pids); do
      cwd="$(readlink -f /proc/$p/cwd 2>/dev/null || true)"
      [ "$cwd" = "$CODER_GUARD_HERE" ] && { exec 9>&-; return 0; }
    done
    alive=""
    # $! survives `disown` (the dispatchers disown their launcher), the job table does not.
    for j in $(jobs -p) ${!:-}; do kill -0 "$j" 2>/dev/null && alive=1; done
    [ -n "$alive" ] || { exec 9>&-; return 0; }
    sleep 0.5
  done
}
coder_guard() {
  local here p cwd busy=""
  here="$(pwd -P)"
  exec 9>"${CODER_GUARD_LOCK:-/tmp/mesh-coder-guard.lock}"
  flock -w 120 9 || { echo "REFUSED: coder admission lock busy"; exit 3; }
  CODER_GUARD_HERE="$here"
  for p in $(coder_pids); do
    cwd="$(readlink -f /proc/$p/cwd 2>/dev/null || true)"; [ -n "$cwd" ] || continue
    case "$cwd" in "$HOME"/workspace/mesh-*) ;; *) continue;; esac
    if [ "$cwd" = "$here" ]; then echo "REFUSED: a coder run is already active in this worktree (pid $p)"; exit 3; fi
    case " $busy " in *" $cwd "*) ;; *) busy="$busy $cwd";; esac
  done
  set -- $busy
  if [ "$#" -ge "${MAX_CODERS:-2}" ]; then echo "REFUSED: $# coder worktrees active (max ${MAX_CODERS:-2}):$busy"; exit 3; fi
  trap coder_guard_release EXIT  # admitted: keep the lock until this coder is visible
}
