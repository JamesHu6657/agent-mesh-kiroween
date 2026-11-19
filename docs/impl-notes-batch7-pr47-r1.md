# Batch 7 PR #47 Round-1 Implementation Notes

This note documents verification against code, `review.md` Repros, and project documentation (`docs/plan.md`, `docs/usage.md`, `docs/requirements.md`, `docs/inbox-workers.md`, `docs/HANDOFF.md`), root-cause fixes, decisions made without user prompting, and test verification for the eight review findings in `review.md` on branch `pr47-r1` (based on `cloud/pr47r1-base`, commit `a4d2a42`).

All eight findings (2x P1, 4x P2, 2x P3) were confirmed real on unedited `a4d2a42`, reproduced with failing regression tests first in [tests/unit/test_batch7_pr47_r1.py](tests/unit/test_batch7_pr47_r1.py) (plus un-masking `PYTHONPATH` in [tests/unit/test_batch7_g6.py](tests/unit/test_batch7_g6.py)), and resolved with minimal root-cause fixes.

---

## Verified Findings and Fixes

### 1. [P1] `scripts/coord/merge.sh` verifies `refs/heads/wt/<N>` yet merges unqualified `wt/<N>`

- **Doc & Code Verification:** Confirmed real against `scripts/coord/merge.sh:20-24` and `tests/unit/test_merge.py` (`PipelineTagShadowTest`, `docs/plan.md` §10.5 coordinator merge gate). Line 20 resolved `H=$(git rev-parse --verify "refs/heads/wt/$N^{commit}")` and line 21 checked `${H:0:7}` in the committed `VERDICT: ACCEPT` review file, but line 24 executed `git merge --no-ff "wt/$N"`. Because Git resolves `refs/tags/wt/<N>` before `refs/heads/wt/<N>` when merging unqualified names (and all worktrees share `.git/refs/tags`), a colliding tag shadowed the reviewed branch; additionally, a commit landing on `refs/heads/wt/$N` between line 20 and merge could be merged without review while line 33 printed `merged wt/$N ($H)`.
- **Root Cause & Fix:**
  - [scripts/coord/merge.sh](scripts/coord/merge.sh): Re-verify `cur_tip=$(git rev-parse --verify "refs/heads/wt/$N^{commit}")` immediately before merging, refuse with exit `2` if `cur_tip != H`, and pass the pinned commit SHA `"$H"` directly to `git merge --no-ff "$H" -m "$MSG (review: $RV)"`.

### 2. [P1] `scripts/coord/secret-pattern.sh` misses JSON-escaped quotes, triple quotes, and Python string prefixes

- **Doc & Code Verification:** Confirmed real against `scripts/coord/secret-pattern.sh:11`, `.kiro/hooks/scripts/secret-scan.sh`, `scripts/coord/push-review.sh`, `scripts/coord/sync-github.sh`, `docs/plan.md` §15.1–§15.2, and `docs/requirements.md` (§49). `secret-pattern.sh` is the sole guard for generic `*_API_KEY`, `BRIGHTDATA_*`, `HERMES_TELEGRAM_BOT_TOKEN`, and `WECHAT_ROUTINE_WEBHOOK_KEY` assignments across all three scanners (`gitleaks` has no generic `*_API_KEY` rule). The prior `'?"?` quote quantifier failed on JSON-escaped quotes (`\"`), triple quotes (`"""..."""`, `'''...'''`), and Python string prefixes (`r"..."`, `b"..."`, `u"..."`, `rb"..."`, `br"..."`).
- **Root Cause & Fix & Decision:**
  - [scripts/coord/secret-pattern.sh](scripts/coord/secret-pattern.sh): Extended key-side quotes to `(\\?['\''"]){0,3}` and value-side prefix/quote wrapper to `(([rRbBuU]|[rR][bB]|[bB][rR])?(\\?['\''"]){1,3})?` before `[^$\` '\''"\\][^` '\''"\\]{5,}`.
  - **Key pitfall avoided:** Did *not* include an ungrouped backslash `\` in the optional wrapper outside `(\\?['"])` (since an escape like `\nmy_secret_val` is not a quote wrapper and must not false-match), while keeping `$VAR`, `` `cmd` ``, empty `""`/`""""""`/`\"\"`, and short (`< 6`-char) placeholders allowed. Verified zero matches against `secret-pattern.sh`, `secret-scan.sh`, `push-review.sh`, `sync-github.sh`, `docs/impl-notes-batch7-pr47-r1.md`, and `test_batch7_pr47_r1.py`, and across `git ls-files` only `scripts/coord/gitleaks-baseline.json:48` (an escaped-JSON `"REDACTED"` match entry, analogous to the existing unescaped baseline lines 128–208) was newly matched.

### 3. [P2] `scripts/coord/migrate-to-paseo.sh` `paseo_auto_accept` is fail-open on non-bools, wildcards, and duplicates

- **Doc & Code Verification:** Confirmed real against `scripts/coord/migrate-to-paseo.sh:16-65` and the fail-closed permission guard documented on line 67 & `docs/HANDOFF.md` §8 (imported Kiro ACP agent must have auto-accept strictly OFF). Any `features` list containing an entry with `id == "auto_accept"` whose `value` was `None`, `"true"`, `1`, `"false"`, `0`, or omitted (e.g., `defaultValue: true`) fell into `elif feats:` -> `"off"`, and `glob.glob(f".../{aid}.json")` accepted `*`, `?`, `[` metacharacters and read only `fs[0]`.
- **Root Cause & Fix & Decision:**
  - [scripts/coord/migrate-to-paseo.sh](scripts/coord/migrate-to-paseo.sh):
    - Enforced strict identifier whitelist `re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", aid)` and `".." not in aid`, escaped with `glob.escape(aid)`, and rejected `len(fs) != 1` as `"ambiguous"`.
    - Wrapped JSON loading in `try/except (OSError, ValueError)` and validated `isinstance` for top-level `dict`, `config`, `config.featureValues`, and `features` (returning `"unknown"` on malformed/unexpected schemas).
    - Checked `"auto_accept" in fv` strictly (returning `"on"` for `True`, `"unknown"` if present and `is not False`), required `feats` non-empty, and printed `"off"` **only** when `all(f.get("value") is False for f in feats)` (and `fv` is absent or `False`); otherwise prints `"on"`, `"missing"`, or `"unknown"`.

### 4. [P2] Unit-test `/proc` lock audit across all 105 `tests/unit/test_*.py` modules

- **Doc & Code Verification:** Confirmed real against `scripts/test.sh:82` and `tests/run_tier.py:90-142`. Running `tests/unit` via `run_tier.py` leaves `DEFAULT_PROC_LOCK = None`. While `test_batch7_g2.py`, `test_blind_review_b34.py`, `test_m3_gatefix.py`, `test_merge.py`, `test_proc_probe.py`, `test_runtargets_race.py`, `test_seen_hold_r4d.py`, `test_seen_hold_r4e.py`, and `test_termination_units.py` already set `MESH_PROC_LOCK = "shared"` (plus `"exclusive"` on three `prctl(PR_SET_DUMPABLE, 0)` classes in `test_m3_gatefix.py`), four real `/proc`-scanning unit modules lacked it:
  1. [tests/unit/test_batch7_pr43_r1.py](tests/unit/test_batch7_pr43_r1.py) (`supervisor._watch_lead_turn`, `supervisor.advance_terminations`, `recovery.recover`)
  2. [tests/unit/test_batch7_pr45_r1.py](tests/unit/test_batch7_pr45_r1.py) (`merge.run_merge_pipeline`, `supervisor.watch_launch_runs`; inherits `tm.TaskBranchCase` from `test_merge`, which failed lookup in `run_tier.proc_lock_mode` before class/module fix)
  3. [tests/unit/test_batch7_pr45_r2.py](tests/unit/test_batch7_pr45_r2.py) (`recovery.recover`, `merge.run_merge_pipeline`)
  4. [tests/unit/test_merge_recovery_minors.py](tests/unit/test_merge_recovery_minors.py) (`recovery._step_merge_runs`; subclass of `tm.TaskBranchCase`)
- **Full 105-file Audit Disposition:**
  - Added `MESH_PROC_LOCK = "shared"` at module level to all 4 unprotected scanners above, plus `mesh_proc_lock = "shared"` directly on `MergeDbCase` and `TaskBranchCase` in [tests/unit/test_merge.py](tests/unit/test_merge.py) so cross-module subclasses inherit `"shared"` automatically.
  - Other `/proc`-mentioning unit files were intentionally left unmarked after inspection confirmed they do **not** perform host-wide `/proc` PID scans: `test_linear_full_daemon.py` (mocks `run_test_step` to raise `OSError` immediately), `test_proc_sibling_scan_cap.py` (mocks `os.listdir`/`os.stat`/`open` on synthetic PIDs), `test_dogfood_merge_held.py` / `test_pr37_review_core.py` / `test_sakana_core.py` (mock `terminate.get_proc_stat`, `_real_uid`, etc. on fake dicts), and `test_reap_daemons.py` / `test_fake_agent_behaviors.py` / `test_smoke_harness.py` / `test_doctor_fix_perms_r6.py` / `test_auth.py` (read only `/proc/self/*` or explicit `child.pid` they spawned).
  - Added an automated AST audit in [tests/unit/test_batch7_pr47_r1.py](tests/unit/test_batch7_pr47_r1.py) (`TestFinding4UnitProcLockAudit`) verifying every unmocked real `/proc`-scanning unit module resolves `proc_lock_mode == "shared"`.

### 5. [P2] `scripts/inbox-worker-example.sh` orphans `$MESH_BIN` heartbeat child by backgrounding shell function `mesh_cmd`

- **Doc & Code Verification:** Confirmed real against `scripts/inbox-worker-example.sh:45, 187`, `docs/inbox-workers.md` §5, and `tests/unit/test_inbox_worker_kit.py:106, 329-333`. Backgrounding the shell function `mesh_cmd ... &` forks an intermediate subshell bash process (`hb_child=$!`) which then forks `"$MESH_BIN"` without `exec`; when `hb_cleanup` kills `"$hb_child"`, the intermediate bash exits and `"$MESH_BIN" task heartbeat` survives as an orphan.
- **Root Cause & Fix & Decision:**
  - [scripts/inbox-worker-example.sh](scripts/inbox-worker-example.sh): Added `_MESH_EXEC=1` support to `mesh_cmd()` (`if [ "${_MESH_EXEC:-0}" = 1 ]; then exec "$MESH_BIN" "$@" --token-file "$MESH_TOKEN_FILE"; fi`) and invoked `_MESH_EXEC=1 mesh_cmd task heartbeat ... &` on line 192. This keeps every mesh invocation flowing through `mesh_cmd` (preserving `test_inbox_worker_kit.py`'s `mesh_cmd <res> <verb>` static check and single `--token-file` wrapper) while making `"$hb_child"` directly equal to the `"$MESH_BIN"` process.

### 6. [P2] `tests/run_tier.py` omits `top_level` from child `PYTHONPATH`, turns module-level `SkipTest` into `ERROR`, and conditionally drops `loader.errors`

- **Doc & Code Verification:** Confirmed real against `tests/run_tier.py:155-185, 241-306` and the masked assertions in `tests/unit/test_batch7_g6.py:335-350`.
- **Root Cause & Fix & Decision:**
  - [tests/run_tier.py](tests/run_tier.py):
    - Resolved `REPO_ROOT`, inserted it into `sys.path`, added `_build_child_pythonpath(top_level, os.environ.get("PYTHONPATH"))`, and passed `PYTHONPATH` into child subprocesses in `_run_single_test` (with an optional `pythonpath: Optional[str] = None` parameter on `run_single_test`/`_run_single_test` preserving backward compatibility with 2-arg callers).
    - Checked `os.path.commonpath([REPO_ROOT, dir_abs]) == REPO_ROOT` **before** `cwd_abs` when selecting `top_level` so invoking `tests/run_tier.py` from a parent directory still roots imports at `REPO_ROOT`.
    - Detected `unittest.loader.ModuleSkipped` / `_ModuleSkipped` in `main()` and recorded `(t_id, "skipped", 0.0, reason)` directly via `case.run(unittest.TestResult())` instead of spawning a failing `python3 -m unittest` subprocess.
    - Deduplicated `loader.errors` against recorded `_FailedTest` error messages rather than discarding all `loader.errors` whenever any `_FailedTest` exists.
  - [tests/unit/test_batch7_g6.py](tests/unit/test_batch7_g6.py): Removed manual `PYTHONPATH` injection on lines 335–350 so `skip_dir` and `err_dir` exercise real unassisted CLI behavior.

### 7. [P3] `scripts/coord/coder-guard.sh` `git worktree list` fails silently under `set -euo pipefail` and misses custom worktree subdirs

- **Doc & Code Verification:** Confirmed real against `scripts/coord/coder-guard.sh` and `scripts/coord/dispatch-{agy,commandcode,droid,vibe}.sh` (all four source `coder-guard.sh` under `set -euo pipefail`), plus `docs/HANDOFF.md` §4/§10. Under `set -euo pipefail`, `{ git worktree list --porcelain 2>/dev/null | sed ... | while read ...; }` returned git's non-zero exit code (e.g., `128`) and killed the sourcing script with empty stdout and stderr. Additionally, `[ "$cwd" = "$wt" ]` missed coders operating inside subdirectories of non-`mesh-*` worktrees.
- **Root Cause & Fix & Decision:**
  - [scripts/coord/coder-guard.sh](scripts/coord/coder-guard.sh): Captured `wt_out="$(LC_ALL=C git worktree list --porcelain 2>&1)" || wt_rc=$?` cleanly outside any pipeline. Degraded open (`worktrees=""`) **only** when `GIT_DIR` and `GIT_WORK_TREE` are unset, `! _coder_guard_in_git_tree "$here"`, and git reported `"not a git repository"`; all other `git worktree list` errors (`dubious ownership`, corrupt `.git`, broken `GIT_DIR`, missing `git`) refuse loudly with `REFUSED: git worktree list failed (...)` and exit `3`.
  - Added `_coder_guard_wt_root` and `"$wt"|"${wt%/}"/*` subdirectory prefix matching (selecting the longest matching worktree path) in both `coder_guard` and `coder_guard_release`.

### 8. [P3] `scripts/inbox-worker-example.sh` skips `SIGTERM` on PGID leftovers and stalls when killed PGID leaves zombies under PID 1

- **Doc & Code Verification:** Confirmed real against `scripts/inbox-worker-example.sh:253-286` (and `docs/inbox-workers.md` §5, review D7). When the main command exited normally (`reaped=1`) with live background descendants in PGID `-$cmd_pid`, `kill -TERM` was guarded by `if [ -e "$lost" ] || [ -n "$stopping" ]`, so normal exits skipped `SIGTERM`, stalled the full `KILL_GRACE_S`, then sent `SIGKILL` (followed by an unreachable dead `kill -TERM` after `SIGKILL`).
- **Container Zombie Discovery & Root-Cause Fix:**
  - Sending `kill -TERM -- "-$cmd_pid"` unconditionally whenever entering the cleanup block revealed a second issue under container PID 1 (observed via `ps -o pid=,ppid=,pgrp=,stat=`: reparented children in PGID `-$cmd_pid` remained `<defunct>` state `Z` while PID 1 did not immediately reap them). In POSIX/Linux, `kill -0 -- "-$cmd_pid"` returns `0` for a PGID containing only `Z` zombies, causing the worker to still stall the entire `KILL_GRACE_S` even after descendants exited on `SIGTERM`.
  - [scripts/inbox-worker-example.sh](scripts/inbox-worker-example.sh): Sent `SIGTERM` unconditionally when entering the cleanup branch, removed the dead post-`SIGKILL` `kill -TERM`, and added `_pgid_has_live "$cmd_pid"` checking `/proc/[0-9]*/stat` in pure bash (with portable `ps -e -o pgid=,stat=` fallback when `/proc` is absent and safe fallback `return 0`), gated on `[ "$reaped" -eq 1 ]`. `SIGTERM`-handling leftovers now finish immediately (`< 2.0s` even with `KILL_GRACE_S=5`), while `SIGTERM`-ignoring (`SIG_IGN`) leftovers still wait bounded `KILL_GRACE_S` and are `SIGKILL`ed.

---

## Verification

### Pre-fix Baseline Failure Confirmation (on unedited `a4d2a42`)

- `python3 -m unittest tests/unit/test_batch7_pr47_r1.py tests/unit/test_batch7_g6.py`: **Ran 28 tests — FAILED (failures=15, errors=1)** prior to production fixes (with `test_branch_move_between_check_and_merge_is_refused` additionally verified failing `0 != 2` by merging unreviewed `late.txt` on unedited `merge.sh`).

### Post-fix Verification Runs

1. **New & un-masked regression tests (3 consecutive runs):**
   - `python3 -m unittest tests/unit/test_batch7_pr47_r1.py tests/unit/test_batch7_g6.py` -> **Ran 28 tests, OK** x3 consecutive runs (`10.496s`, `12.478s`, `10.547s`; 15 tests in `test_batch7_pr47_r1.py` + 13 tests in `test_batch7_g6.py`).
2. **Related script, worker-kit, tierlib, and review unit suites:**
   - `python3 -m unittest tests/unit/test_batch7_pr47_r1.py tests/unit/test_batch7_g6.py tests/unit/test_inbox_worker_kit.py tests/unit/test_sakana_core.py tests/unit/test_linear_full_scripts.py tests/unit/test_tierlib.py tests/unit/test_pr39_r1.py tests/unit/test_pr39_r2.py` -> **Ran 103 tests, OK** (`80.049s`).
3. **Newly `/proc`-locked & related unit suites:**
   - `python3 -m unittest tests/unit/test_batch7_g2.py tests/unit/test_blind_review_b34.py tests/unit/test_runtargets_race.py tests/unit/test_seen_hold_r4d.py tests/unit/test_seen_hold_r4e.py tests/unit/test_dogfood_merge_held.py tests/unit/test_pr37_review_core.py` -> **Ran 149 tests, OK** (`36.795s`).
   - `python3 -m unittest tests/unit/test_merge.py tests/unit/test_merge_recovery_minors.py tests/unit/test_batch7_pr43_r1.py tests/unit/test_batch7_pr45_r1.py tests/unit/test_batch7_pr45_r2.py tests/unit/test_proc_probe.py tests/unit/test_termination_units.py` -> **Ran 74 tests, 73 passed, 1 pre-existing clock-expiry failure** (`test_merge.py`: `test_duplicate_idempotency_key_keeps_the_mutation` hardcodes `NOW = "2026-11-09T00:00:00Z"`, which exceeds `IDEMPOTENCY_TTL_S = 7 * 86400` when run on `2026-11-16`; identical on `cloud/pr47r1-base`, left untouched as out of scope).
4. **Related integration suites (full suite `scripts/test.sh` intentionally skipped per task instructions):**
   - `python3 -m unittest tests/integration/test_worker_friction.py tests/integration/test_sakana_cli.py tests/integration/test_sakana_core.py tests/integration/test_merge_refs.py tests/integration/test_refs_worktrees.py tests/integration/test_execution_inbox.py` -> **Ran 65 tests, OK** (`141.043s`).
   - `python3 -m unittest tests/integration/test_batch7_pr43_r1.py tests/integration/test_batch7_pr45_r1.py tests/integration/test_batch7_pr45_r2.py tests/integration/test_pr39_r1.py tests/integration/test_pr39_r2.py` -> **Ran 20 tests, OK** (`45.372s`).
5. **Shell syntax & self-scan hygiene:**
   - `bash -n` clean on all edited scripts; `secret-pattern.sh` grep clean (0 hits) across all edited/new code, tests, and documentation; `scripts/test.sh --check` OK; `git diff --check` clean.

