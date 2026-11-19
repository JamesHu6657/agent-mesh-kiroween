# Batch 7 PR #47 Round-3 Implementation Notes

This note documents verification against repository code and `review.md` Repros, root-cause fixes, decisions made without user prompting, and test verification for the two round-3 review findings on branch `pr47-r3` (based on `cloud/pr47r3-base`, commit `a29c141`).

Both P2 findings were confirmed real on unedited `a29c141`, reproduced with failing-first regression tests in [tests/unit/test_batch7_pr47_r3.py](../tests/unit/test_batch7_pr47_r3.py), and resolved with minimal root-cause fixes. A restack-induced `/proc`-lock omission in [tests/unit/test_batch7_pr45_r3.py](../tests/unit/test_batch7_pr45_r3.py) surfaced when running `tests/unit/test_batch7_pr47_r1.py` and was also aligned.

---

## Verified Findings and Fixes

### 1. [P2] `scripts/inbox-worker-example.sh` treats multithreaded `pthread_exit` leader-zombies as dead, exits `pgid_ready` early without `/proc`, and unconditionally sets `reaped=1` on trap-interrupted `wait`

- **Verification against Code & `review.md` Repro:** Confirmed real on `a29c141` at lines 72–119 and 281–354 (comparing against `is_tgid_alive` in [mesh/core/proc.py](../mesh/core/proc.py)):
  1. When a multithreaded command (or background process in its PGID) has its main thread call `pthread_exit` / `SYS_exit` while non-daemon worker threads remain alive, `/proc/<pid>/stat` reports state `Z` with `num_threads > 1` (`ps` reports `Zl` / `nlwp > 1`) and live entries remain under `/proc/<pid>/task/*/stat`. `_pgid_has_live` skipped `Z|X|x` (and `Z*` in `ps`), and the main/grace loops set `cmd_dead=1` and entered blocking `wait "$cmd_pid"` whenever state was `Z` or `""`. On lease loss (`SIGUSR1`) or `SIGTERM`/`SIGINT`, `_signal_cmd` skipped `"$cmd_pid" "-$cmd_pid"`, `_pgid_has_live` returned false, `wait "$cmd_pid"` was interrupted (`rc=138`/`143`) and still set `reaped=1` unconditionally, and the grace/kill block was skipped completely.
     - Exact `review.md` Repro on `a29c141`: `worker rc = 9 | child still alive after lease lost = True`.
  2. When `/proc/$$/stat` is unreadable (no `/proc`), `st=""` caused the `pgid_ready` wait loop to break on the first iteration before `os.setpgid(0, 0)`, and caused the main loop to enter blocking `wait "$cmd_pid"` and mark `reaped=1` even if interrupted by a trapped signal (`rc > 128`).
- **Root Cause & Fix:**
  - [scripts/inbox-worker-example.sh](../scripts/inbox-worker-example.sh):
    - Added `_proc_tgid_alive` matching `mesh/core/proc.py:is_tgid_alive`: reads `/proc/$pid/stat` and, when state is `Z|X|x`, treats the thread-group as alive if stat field 20 (`num_threads`, `${18}` after `*) `) `> 1`, `/proc/$pid/status` `Threads:` `> 1`, or any `/proc/$pid/task/[0-9]*/stat` entry is outside `Z|X|x`.
    - Added `_pid_is_dead`: uses `_proc_tgid_alive` when `/proc/$$/stat` is readable; otherwise checks `ps -o nlwp=,stat= -p "$pid"` (treating `Z*|X*|x*` with `nlwp > 1` as alive), falls back to BSD-compatible `ps -o stat= -p "$pid"`, and finally `! kill -0 "$pid" 2>/dev/null`.
    - Updated `_pgid_has_live` to check `_proc_tgid_alive "$pid"` on `Z|X|x` entries in `/proc/[0-9]*/stat`, and added `ps -e -o pgid=,nlwp=,stat=` ahead of the `ps -e -o pgid=,stat=` fallback.
    - Added `_cmd_reaped(pid, wrc)`: treats `wrc == 127` or `wrc <= 128` as reaped; when `wrc > 128`, requires `$pid` to be absent from `jobs -p` and `_pid_is_dead "$pid"` to succeed before confirming reap (otherwise callers reset `cmd_dead=0` and keep `reaped=0`).
    - Updated `_signal_cmd`, the `pgid_ready` wait loop, main loop, grace loop, and final `SIGKILL` reap loop (`while :; do wait "$cmd_pid" 2>/dev/null; wrc=$?; _cmd_reaped "$cmd_pid" "$wrc" && break; done`) to use `_pid_is_dead` and `_cmd_reaped` while preserving all round-2 post-reap and `pgid_gone` guards.
- **Decisions:**
  - Inspected all three thread-count sources in `_proc_tgid_alive` (`stat` field 20, `status` `Threads:`, and `task/*/stat`) so `/proc` edge cases (or partial `/proc` trees in tests) behave identically to `mesh/core/proc.py:is_tgid_alive`.
  - Retained `ps -o stat=` / `ps -e -o pgid=,stat=` after `nlwp` probes so BSD/macOS `ps` implementations without `nlwp` still avoid false fallbacks.
  - Gated `_cmd_reaped`'s job/liveness re-check on `wrc > 128` (trap interruption exit range) combined with `! jobs -p` and `_pid_is_dead` so children that actually die from signals (e.g., exit `137`/`143`) still report reaped and preserve `cmd_rc`.
  - Marked [tests/unit/test_batch7_pr47_r3.py](../tests/unit/test_batch7_pr47_r3.py) with `MESH_PROC_LOCK = "shared"` per `TestFinding4UnitProcLockAudit` and gated live `pthread_exit` tests with `_supports_leader_zombie()` alongside a deterministic fake-`/proc` + `_cmd_reaped` unit test.

### 2. [P2] `tests/run_tier.py` mismatches symlinked paths against `getcwd()` and lifts `top_level_dir` on invalid/keyword package directories

- **Verification against Code & `review.md` Repro:** Confirmed real on `a29c141` at lines 32, 37–53, and 268–281:
  1. `REPO_ROOT`, `cwd_abs`, `dir_abs`, and `_build_child_pythonpath` used `os.path.abspath` / `os.path.normpath`. Under a symlinked working directory or symlinked repo invocation (e.g., `$PWD/ext_pkg/sub` under `sym_w -> real_w`), `os.path.abspath(".")` returned the physical path while `os.path.abspath(test_dir)` retained the logical symlink path, causing `os.path.commonpath` to fail to match and leaving `top_level` unlifted (`ModuleNotFoundError`).
  2. `_is_importable_under` checked only `__init__.py` without validating whether each package directory segment was a valid non-keyword Python identifier, lifting `top_level` above directories like `rel.1/` or `class/` and failing `unittest` discovery with `ModuleNotFoundError`.
  - Exact `review.md` Repros on `a29c141`: `symlink $PWD rc = 1` and `dotted dir rel.1 rc = 1`.
- **Root Cause & Fix:**
  - [tests/run_tier.py](../tests/run_tier.py):
    - Imported `keyword` and normalized `REPO_ROOT`, `cwd_abs`, `dir_abs`, `_is_importable_under` inputs, and `_build_child_pythonpath` entries with `os.path.realpath`.
    - Updated `_is_importable_under` to reject any component above `root` where `not pkg.isidentifier() or keyword.iskeyword(pkg)`.
    - Passed `discover_target = dir_abs if os.path.isdir(dir_abs) else test_dir` into `loader.discover(discover_target, top_level_dir=top_level)` so existing directories use matching physical paths while nonexistent paths continue to raise and report `discovery_error` cleanly.
- **Decisions:**
  - Used `str.isidentifier() and not keyword.iskeyword()` instead of an ASCII regex so valid Unicode package names and Python soft keywords (such as `match`) remain importable while hard keywords and dotted/hyphenated directory names never lift `top_level`.

### 3. Restack `/proc`-Lock Alignment in `tests/unit/test_batch7_pr45_r3.py`

- **Finding:** Running the mandated `tests/unit/test_batch7_pr47_r1.py` suite on `a29c141` failed in `TestFinding4UnitProcLockAudit.test_proc_scanning_modules_resolve_shared_lock_in_run_tier` because restacking PR #47 onto PR #46 brought in [tests/unit/test_batch7_pr45_r3.py](../tests/unit/test_batch7_pr45_r3.py), which invokes `merge.run_merge_pipeline(...)` (scanning real `/proc` via `terminate.run_targets_or_recheck`) without declaring `MESH_PROC_LOCK = "shared"`.
- **Fix & Decision:** Added `MESH_PROC_LOCK = "shared"` at module level in [tests/unit/test_batch7_pr45_r3.py](../tests/unit/test_batch7_pr45_r3.py) (matching `test_batch7_pr45_r1.py`, `test_batch7_pr45_r2.py`, and `test_batch7_pr47_r3.py`), and verified `run_tier.proc_lock_mode` resolves `"shared"` for all tests in both `test_batch7_pr47_r3` and `test_batch7_pr45_r3`.

---

## Verification

### Pre-fix Baseline Failure Confirmation (on unedited `cloud/pr47r3-base` @ `a29c141`)

- Exact `review.md` Repros on `a29c141`:
  - `worker rc = 9 | child still alive after lease lost = True`
  - `symlink $PWD rc = 1`
  - `dotted dir rel.1 rc = 1`
- `python3 -m unittest -v tests/unit/test_batch7_pr47_r3.py` against a clean detached worktree at `a29c141`: **Ran 6 tests — FAILED (failures=5, errors=2)** (all 6 regression test methods failed on unedited code).

### Post-fix Verification Runs

1. **Exact `review.md` Repros:**
   - `worker rc = 9 | child still alive after lease lost = False`
   - `symlink $PWD rc = 0`
   - `dotted dir rel.1 rc = 0`
2. **New regression tests (3 consecutive runs):**
   - `python3 -m unittest tests/unit/test_batch7_pr47_r3.py`: **Ran 6 tests, OK** x3 consecutive runs (`10.439s`, `10.393s`, `10.422s`).
3. **Related unit suites (full suite `scripts/test.sh` omitted per task instructions):**
   - `python3 -m unittest tests/unit/test_batch7_pr47_r3.py tests/unit/test_batch7_pr47_r2.py tests/unit/test_batch7_pr47_r1.py tests/unit/test_batch7_g6.py tests/unit/test_inbox_worker_kit.py tests/unit/test_batch7_pr45_r3.py tests/unit/test_tierlib.py tests/unit/test_batch6_shutdown.py tests/unit/test_proc_probe.py tests/unit/test_pr39_r1.py tests/unit/test_pr39_r2.py`: **Ran 131 tests in 96.994s — OK**.
   - Confirmed `run_tier.proc_lock_mode` resolves `'shared'` for `tests.unit.test_batch7_pr47_r3` (6 cases) and `tests.unit.test_batch7_pr45_r3` (15 cases).
4. **Syntax, permissions, and secret-pattern hygiene:**
   - `bash -n scripts/inbox-worker-example.sh`, `test -x scripts/inbox-worker-example.sh`, `bash scripts/test.sh --check`, `git diff --check cloud/pr47r3-base`: all pass (`rc=0`).
   - `grep -En "$P"` using [scripts/coord/secret-pattern.sh](../scripts/coord/secret-pattern.sh) across `scripts/inbox-worker-example.sh`, `tests/run_tier.py`, `tests/unit/test_batch7_pr47_r3.py`, `tests/unit/test_batch7_pr45_r3.py`, and `docs/impl-notes-batch7-pr47-r3.md`: 0 hits.
