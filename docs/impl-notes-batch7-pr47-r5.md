# Batch 7 PR #47 Round-5 Implementation Notes

Fixes for the two round-5 review findings on `48130a6` (`scripts/inbox-worker-example.sh` and its unit tests).

---

## 1. [P2] Never defer to `ps` when a readable `/proc` scan hits an unreadable entry

- **Problem:** When `/proc/$$/stat` was readable (`[ -r "/proc/$$/stat" ]`), `_pgid_has_live` confirmed the process group still existed via `kill -0 -- "-$pgid"`, scanned `/proc`, hit an unreadable entry (`unreadable=1`), and then fell through to `_pgid_has_live_ps`. On Linux, `procps-ng` / BusyBox `ps` runs in the same security context, reads the same `/proc/<pid>/stat`, and silently skips `EACCES` entries—returning `1` ("empty") when the leader had exited (`reaped=1`) and only an unreadable grandchild remained in `-$pgid`. Additionally, globbing `/proc/[0-9]*/stat` skipped `0o000` `/proc/<pid>` directories before the `read` error check could see them.
- **Fix ([scripts/inbox-worker-example.sh](../scripts/inbox-worker-example.sh)):**
  - In `_pgid_has_live`: inside the `[ -r "/proc/$$/stat" ]` branch, return `0` (live, fail-closed) immediately whenever `unreadable=1`, without calling `_pgid_has_live_ps`.
  - Scan `/proc/[0-9]*` (checking `[ -d "$d" ]` on `read < "$d/stat"` failure) so `0o000` `/proc/<pid>` directories also set `unreadable=1`; similarly check `/proc/$pid/task` readability and iterate `/proc/$pid/task/[0-9]*` in `_proc_tgid_alive`.
  - Keep escalation strictly bounded: the `KILL_GRACE_S` loop runs a fixed `KILL_GRACE_S * 5` iterations before sending `SIGKILL`, and the post-`SIGKILL` PGID wait on `-$cmd_pid` is capped at 10 x 0.05 s iterations (0.5 s max, re-sending `SIGKILL` once at step 5 and breaking in 1 s if fractional `sleep` is unavailable) before setting `pgid_gone=1`—so even if a zombie and unreadable `/proc` entry persist simultaneously, nothing can loop forever.

## 2. [P3] Separate `-o` columns in `_pgid_has_live_ps` and non-vacuous `ps` fallback tests

- **Problem:** `_pgid_has_live_ps` passed combined specs (`-o pgid=,nlwp=,stat=` and `-o pgid=,stat=`). Per POSIX `ps(1p)`, BSD `ps`, macOS `ps`, and OpenBSD `ps`, everything after the first `=` in a `-o` argument is that single field's header text, so only one column (`pgid`) was emitted with header `,nlwp=,stat=` (never even failing on `nlwp` to reach the BSD fallback), leaving `st=""` which fell into `*) return 0 ;;` and stalled zombie-only cleanup for the full `KILL_GRACE_S`. Also, `test_no_proc_ps_fallback_uses_posix_A` did not parse `-o` per POSIX and passed vacuously even if `ps` always exited `1`.
- **Fix ([scripts/inbox-worker-example.sh](../scripts/inbox-worker-example.sh)):**
  - Split every `-o` argument into separate options: `ps -A -o pgid= -o nlwp= -o stat=`, `ps -A -o pgid= -o stat=`, and `ps -ax -o pgid= -o stat=`.
  - Audited the rest of `scripts/inbox-worker-example.sh`: no other `ps` or `-o a=,b=` invocations or `/proc` -> `ps` fallbacks exist.
  - Added `"" ) return 2 ;;` in both `case "$st"` blocks (so an empty `st` on a matching row returns `2` unknown) and required at least one numeric `pgid` row (`saw_row=1`) before returning `1` ("empty").

## 3. Tests (Fail Before on `48130a6`, Pass After)

- Added [tests/unit/test_batch7_pr47_r5.py](../tests/unit/test_batch7_pr47_r5.py) (`MESH_PROC_LOCK = "exclusive"`):
  - `test_unreadable_proc_entry_after_leader_reaped_never_defers_to_ps`: leader exits and is reaped while a `SIGTERM`-ignoring grandchild holds an `flock` with `0o000` fake `/proc/<gp>/stat` and a fake `ps` on `PATH` reading the same fake `/proc`. Asserts grandchild's `flock` is released before worker exit and `ps` is never invoked (both normal exit and lease loss).
  - `test_unreadable_proc_pid_dir_and_persistent_unreadable_termination`: `0o000` `/proc/<gp>` directory under an isolated `PR_SET_CHILD_SUBREAPER` wrapper so both the zombie and unreadable `/proc` entry persist after `SIGKILL`; asserts grandchild is killed and worker terminates promptly.
  - `test_pgid_has_live_ps_separate_o_columns_and_empty_stat_unknown`: verifies no combined `-o a=,b=` flags remain in code and directly tests `_pgid_has_live_ps` against strict POSIX `ps` (Linux `nlwp`, BSD `-A`, OpenBSD `-ax`), truncated/empty `st` rows (`rc=2`), header-only output (`rc=2`), and failing `ps` (`rc=2`).
  - `test_no_proc_zombie_only_pgid_detected_fast_and_not_vacuous`: creates a real zombie-only PGID via `PR_SET_CHILD_SUBREAPER` with `/proc` absent and `KILL_GRACE_S=5`; asserts `< 1.5 s` cleanup across `linux_nlwp`, `bsd_A`, and `openbsd_ax` modes, and verifies an always-failing `ps` takes `>= 4.5 s` (proving non-vacuity).
- Strengthened `test_no_proc_ps_fallback_uses_posix_A` in [tests/unit/test_batch7_pr47_r4.py](../tests/unit/test_batch7_pr47_r4.py) with POSIX `-o` (`split("=", 1)`) parsing, `KILL_GRACE_S=5` zombie-only fast cleanup (`< 1.5 s`), and always-failing `ps` non-vacuity check (`>= 4.5 s`).
- Verified via `git stash` against unedited `48130a6`: all 5 tests (8 failure cases across subtests) in `tests.unit.test_batch7_pr47_r5` + `test_no_proc_ps_fallback_uses_posix_A` failed before the fix and pass after restoring the fix.
