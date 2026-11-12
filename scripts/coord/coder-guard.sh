# Sourced by dispatch-*.sh. Per-worktree one-coder guard + global cap (James 2026-11-08 13:29: max 2 concurrent coders,
# one per git worktree). A coder "belongs" to the worktree that is its cwd; only Mesh repo/worktrees count.
# One coder run can be several processes (timeout wrapper + CLI), so the cap counts distinct worktrees.
coder_pids() { pgrep -f '^agy -p|vibe-rs.*--prompt-file|command-code.* -p |droid exec -f ' || true; }
coder_guard() {
  local here p cwd busy=""
  here="$(pwd -P)"
  for p in $(coder_pids); do
    cwd="$(readlink -f /proc/$p/cwd 2>/dev/null || true)"; [ -n "$cwd" ] || continue
    case "$cwd" in "$HOME"/workspace/mesh-*) ;; *) continue;; esac
    if [ "$cwd" = "$here" ]; then echo "REFUSED: a coder run is already active in this worktree (pid $p)"; exit 3; fi
    case " $busy " in *" $cwd "*) ;; *) busy="$busy $cwd";; esac
  done
  set -- $busy
  if [ "$#" -ge "${MAX_CODERS:-2}" ]; then echo "REFUSED: $# coder worktrees active (max ${MAX_CODERS:-2}):$busy"; exit 3; fi
}
