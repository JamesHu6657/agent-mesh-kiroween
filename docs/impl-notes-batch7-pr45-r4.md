# Batch 7 PR #45 Review R4 Implementation Notes

This note records verification against the codebase, the round-4 review (`review.md`), and project documentation (`docs/requirements.md` §5, §8; `docs/usage.md` §8; `docs/plan.md` §10.5, §10.7, §11.7, §13.4, §17.1), plus root-cause fixes, design trade-offs, related-code audit findings, crash-point state tables, and verification results on branch `pr45-r4` (branched from `cloud/pr45r4-base` commit `08708f2`).

All five round-4 findings (`P1-1`, `P1-2`, `P2-3`, `P2-4`, `P2-5`) were confirmed real against `08708f2` and covered by failing-first regression tests in `tests/unit/test_batch7_pr45_r4.py` (13 unit tests), `tests/integration/test_batch7_pr45_r4.py` (3 end-to-end daemon/subprocess tests), and the tightened assertions in `tests/unit/test_batch7_pr45_r3.py:255-258`.

---

## Verified and Fixed Findings

### 1. P1-1 — Test commits / `git checkout` / cleared `MERGE_HEAD` leaving rogue commits on integration branch, wedging worktree recovery, and mis-reconciling on crash
- **Verification against code and docs:** `docs/requirements.md` (§5) and `docs/plan.md` (§10.5, §11.7 step 5) require failed or aborted merges to leave the integration branch at `pre_head` with a clean worktree, and boot recovery to reconcile to `merged` only when Mesh itself committed the approved merge. On `08708f2` (`mesh/daemon/merge.py`):
  1. A `test_command` running `git commit` (or `git checkout -b ...` + commit) advanced `refs/heads/<integration_branch>` (or switched `HEAD`) and cleared `MERGE_HEAD`. `_abort_and_verify` saw `git merge --abort` exit 128 (`MERGE_HEAD` missing), never reset `HEAD` / branch ref back to `pre_head`, and ran `_restore_tracked_from_ref(integ, ref=pre_head)` against an index still pointing to the rogue commit, leaving the worktree dirty (`D <merged_files>`, `restored=False`), leaving unreviewed commits on `main`, and causing later merges of the same branch to hit `merge-base --is-ancestor` `noop: True`.
  2. A `test_command` running `git merge --abort` + `git add extra.txt` left `AD extra.txt` in `.git/index` (`restored=False`) because neither `_abort_and_verify` nor `decide_recovery` restored the pre-merge index.
  3. On crash recovery before TX2, `decide_recovery` reconciled any merge commit whose `parents[1] == tip` and `task_id in body` as `outcome="merged"`, allowing a test commit mentioning `task_id` (e.g., `Merge branch 'mesh/.../<task_id>'` or a spoofed title) to commit unreviewed files as merged.
  4. `tests/unit/test_batch7_pr45_r3.py:251-254` checked only `outcome`, `reason`, and `"merge_commit" not in out_c` for its `script_commit` case, missing `restored is True`, `head == self.base`, `not has_merge_head`, and `is_clean`.
- **Design decision & trade-off (`git update-ref` + atomic `.git/HEAD`/`.git/index` snapshots vs `git reset --hard`):**
  - Although the task summary mentioned `git reset --hard pre_head`, `reset` is explicitly listed in `FORBIDDEN_SUBCOMMANDS` in `mesh/core/gitops.py` (`docs/plan.md` §10.5 A6 allow-list). Rather than widening the allow-list, the fix stays strictly within A6 (`rev-parse`, `update-ref`, `diff`, `status`, `cat-file`, `merge`, `commit`).
  - Trade-off: restoring `.git/index` and `.git/HEAD` relies on atomic file replacement using paths resolved via `git rev-parse --git-path` (valid for standard and linked git worktrees); any extra branch ref created by a rogue `git checkout -b <stray>` is left as an unattached ref while `HEAD` and `refs/heads/<integration_branch>` are restored cleanly to `pre_head`.
- **Root-cause fixes (`mesh/daemon/merge.py`, `tests/unit/test_batch7_pr45_r3.py`):**
  - Added `_git_path`, `_head_symref`, `_restore_index_bytes`, `_restore_head_and_ref`, and persistent artifacts `pre_merge.json` (`task_id`, `pre_head`, `tip`, `symref`, `head_raw`), `pre_merge.index`, and `commit_ready.json` inside `run_dir`.
  - In `_run_merge_pipeline`: captures and persists `pre_merge.json` and `pre_merge.index` alongside `pre_untracked.json` and `pre_empty_dirs.json` before `git merge --no-ff --no-commit`; checks `HEAD` symref (`pre_symref`) in `_post_test_dirty_paths` and the pre-commit guard; writes `commit_ready.json` (`_save_commit_ready`) immediately before `git commit --no-edit -m <msg>` (and unlinks it on commit failure or exception); sets `committed = True` immediately after `cp.returncode == 0` before `_maybe_crash_after_commit` (matching `_merge_commit` semantics while preventing cleanup rollback after a committed `after_merge_commit` crash); ensures `_GitFailed`/`Exception` handlers run `_abort_and_verify` whenever `pre_head and not committed` and either `_merge_head_present(integ)` or `pre_untracked is not None` (recovering cleanly from test commits followed by unexpected exceptions).
  - In `_abort_and_verify`: when `not _merge_head_present(integ)`, restores `HEAD` attachment/branch ref back to `pre_head` via `_restore_head_and_ref`, restores `.git/index` from `pre_merge.index` via `_restore_index_bytes`, clears hide flags, restores tracked files from `pre_head`, cleans empty-snapshot gitlinks and new untracked artifacts, and verifies `head == pre_head`, `symref_ok`, `not dirty`, and `not merge_head`.
  - In `decide_recovery`: requires `commit_ready.json` (matching `task_id`, `parents == [cr_pre, tip]`, `symref`, and exact `"Merge mesh task <task_id>: <title>"` message) whenever `commit_ready.json` or any pipeline snapshot (`pre_merge.json`, `pre_merge.index`, `pre_untracked.json`, `pre_empty_dirs.json`) is present; preserves the legacy fallback only when `run_dir` has no snapshots (`test_merge.py` / `test_merge_recovery_minors.py` legacy callers); when `pre_merge.json` / `pre_merge.index` exists without a valid `commit_ready` match, routes through `_abort_and_verify` to roll back rogue commits/staged state to `pre_head`.
  - Added the four missing assertions (`restored is True`, `head == self.base`, `not has_merge_head`, `is_clean`) to `tests/unit/test_batch7_pr45_r3.py:255-258`.

### 2. P1-2 — `--skip-worktree` / `--assume-unchanged` index hide flags bypassing dirty checks and wedging subsequent merges
- **Verification against code and docs:** `docs/plan.md` (§10.5) requires detecting any post-test worktree modification and leaving `integ` clean. On `08708f2`, both `_dirty_paths` and `_post_test_dirty_paths` relied on `git status --porcelain=v1` and `_staged_index_snapshot` (`git diff --cached --raw`). Git ignores worktree edits on paths carrying `CE_VALID` (`0x8000`, `--assume-unchanged`) or extended `CE_SKIP_WORKTREE` (`0x4000`, `--skip-worktree`), and `diff --cached` inspects only `(mode, oid, status)`, letting corrupted worktree files pass tests, pollute all subsequent test runs, and fail later merges of those files with `git_failed` (exit 1).
- **Design decision & trade-off (stdlib DIRC binary parser vs CLI `git ls-files` / `git update-index`):**
  - `ls-files`, `update-index`, and `hash-object` are all outside `gitops.py`'s A6 allow-list. Using stdlib `struct` + `hashlib` on the index path resolved via `git rev-parse --git-path index` requires zero allow-list expansion, supports DIRC index versions `2`, `3`, and `4` (including Git MSB-first varint prefix decompression for v4 path compression), validates both SHA-1 (20-byte) and SHA-256 (32-byte) index trailers, and updates the checksum atomically when clearing hide flags on abort/recovery.
  - Pre-merge `_dirty_paths` rejects any pre-existing hide flag (or unreadable/corrupt `.git/index`) as `integration_dirty` without mutating user flags during read-only checks. Only newly introduced `au`/`sw` bits are flagged post-test against `pre_test_flags`, and `_clear_index_hide_flags` clears only hide bits (leaving entry byte layouts intact) prior to `_restore_tracked_from_ref` during abort or recovery.
- **Root-cause fixes (`mesh/daemon/merge.py`):**
  - Added `_parse_index_flags`, `_read_index_flags`, and `_clear_index_hide_flags`.
  - Updated `_dirty_paths`, `_post_test_dirty_paths` (normal and status-failure fallback paths), and the pre-commit guard to snapshot (`pre_test_flags`) and fail closed on newly set hide flags or corrupt/missing `.git/index`.
  - Updated `_abort_and_verify` (including `git merge --abort` retry path) and `decide_recovery` (both snapshot and no-snapshot paths) to clear hide flags before `_restore_tracked_from_ref`.

### 3. P2-3 — `160000` gitlink symlink pollution in `skip_paths_real` and leaked files inside uninitialized empty gitlink dirs
- **Verification against code and docs:** Confirmed both vulnerabilities in `mesh/daemon/merge.py` against `review.md`:
  1. `_gitlink_skip_paths_real` called `os.path.realpath(os.path.join(integ, *parts))` blindly without `lstat`/symlink checks. Replacing an uninitialized gitlink dir (`vendor_sub`) with `vendor_sub -> .` inserted `integ_real` (or a tracked subdir like `tests_real`) into `skip_paths_real`, making `make_owned_dirs_writable` skip `0o000` directories elsewhere in the worktree and causing `_restore_tracked_from_ref` to fail (`restored=False`).
  2. `_cleanup_new_untracked`, `_cleanup_empty_dirs`, and `_post_test_dirty_paths` exempted every path under a `160000` gitlink unconditionally. In standard uninitialized integration worktrees where tracked gitlinks start as empty dirs (recorded in `pre_empty_dirs` / `pre_test_empty_dirs`), test artifacts written inside `vendor_sub/` (`vendor_sub/leaked.txt`, `vendor_sub/subcache/d000/`) were ignored and persisted on disk across merges until colliding with future branch changes.
- **Root-cause fixes (`mesh/daemon/merge.py`):**
  - Added `_is_safe_gitlink_dir` (walks path components without following symlinks and verifies containment inside `integ_real`) and `_tampered_gitlink_paths` (detects symlinked/non-dir gitlinks or ancestors in `_dirty_paths`, `_post_test_dirty_paths`, and pre-commit guard).
  - Updated `_gitlink_skip_paths_real` to include a gitlink only when `_is_safe_gitlink_dir(integ, g)` holds.
  - Added `_cleanable_and_exempt_gitlinks`, `_cleanup_inside_empty_gitlinks`, and `_non_empty_cleanable_gitlinks` using `empty_snapshot` (`pre_test_empty_dirs` or `pre_empty_dirs`):
    - Populated/initialized submodules (not in `empty_snapshot`, or when `empty_snapshot is None`) stay `exempt`, keeping all contents and permissions untouched.
    - Empty-snapshot gitlinks (`g in empty_snapshot`) are `cleanable`: `<integ>/<g>` itself is preserved (never `rmdir`'d), while any child files/subdirs/`0o000`/`.git` dirs written inside it are made writable and recursively deleted in `_cleanup_new_untracked`, `_abort_and_verify`, and `decide_recovery` (no-snapshot path), and any leftover contents are flagged as dirty (`vendor_sub`).

### 4. P2-4 — `plan_workspace_prune` / `--dry-run` / prune `except BaseException:` running synchronous git in event loop & `BEGIN IMMEDIATE` TX, plus missing `integ_wt` false cleanup
- **Verification against code and docs:** `docs/plan.md` (§10.7, §11.7) and PR #45 r1–r2 established that blocking git calls must stay off the asyncio event loop and outside `BEGIN IMMEDIATE` write transactions. On `08708f2`:
  1. `plan_workspace_prune(ctx)` (`mesh/daemon/handlers/run.py`) called `_resolve_integ_wt_from_values` (`git worktree list --porcelain`) and `_already_pruned` (`git show-ref --verify`) for every historical `merged`/`cancelled` task row during TX1 (on the event loop holding SQLite's write lock), causing linear git subprocess growth per historical task.
  2. `DaemonServer.handle_client` (`mesh/daemon/server.py:1814-1819`) called `plan_workspace_prune(ctx)` inside the `asyncio.to_thread(...)` argument expression, which still evaluated synchronously on the event loop before spawning the worker thread (making it fragile to future git/I/O additions unless kept explicitly SQLite-only and clearly sequenced).
  3. `run_workspace_prune`'s `except BaseException:` (`mesh/daemon/handlers/run.py`) opened `conn.execute("BEGIN IMMEDIATE;")` and ran `reconcile_prune_checkpoint` synchronously on the loop inside the TX.
  4. `_remove_worktree_and_branch_with_roots` skipped `git branch -d` and returned `None` (reporting success and emitting `workspace.pruned`) when `branch` was non-empty and `integ_wt` was missing/non-dir, even though `_already_pruned` returned `False` under the same condition.
- **Root-cause fixes (`mesh/daemon/handlers/run.py`, `mesh/daemon/server.py`):**
  - Restored `plan_workspace_prune(ctx)` to pure SQLite + local `os.path.lexists` / `_match_task_workspace_with_roots` checks (**zero** `git` calls), removing `_resolve_integ_wt_from_values` and `_already_pruned` from TX1 planning.
  - Updated `mesh/daemon/server.py:1815-1819` (`workspace.prune and dry_run`) to build DB-only `prune_plan = run_handlers.plan_workspace_prune(ctx)` before offloading `await asyncio.to_thread(run_handlers.execute_workspace_prune_plan, prune_plan)`.
  - Updated `execute_workspace_prune_plan`: resolves and caches `integ_wt` per `(project_id, matched_root)` on the worker thread, filters out `_already_pruned` tasks into `active_items`, records `active_planned_ids` in the checkpoint immediately before the loop and in every subsequent checkpoint update, and avoids scanning historical tasks during reconciliation.
  - Split `reconcile_prune_checkpoint` into pure off-loop/off-TX `reconcile_prune_checkpoint_from_data(allowed_roots, default_root, proj_meta, doc, ckpt)` plus `_collect_prune_project_meta`; short-circuits cleanly to `removed=[]`, `completed=False` when `"active_planned_ids" in ckpt and ckpt["active_planned_ids"] is None` (crash before worker filtering).
  - Updated `run_workspace_prune`: writes `"active_planned_ids": None` into TX1's initial checkpoint; in `except BaseException:`, reads SQLite metadata *before* opening a TX, executes `await asyncio.to_thread(reconcile_prune_checkpoint_from_data, ...)` outside any transaction and off the event loop, then opens `BEGIN IMMEDIATE;` strictly to emit deduplicated `(task_id, request_id)` `workspace.pruned` events and delete `ws_res:*`.
  - Updated `_remove_worktree_and_branch_with_roots`: checks `if branch and not (integ_wt and os.path.isdir(integ_wt)): return "branch_delete_failed"` **before** removing `real_path`, guaranteeing missing `integ_wt` never silently drops unremovable branches or removes only the worktree dir.

### 5. P2-5 — `run_workspace_delete` Phase 2 exception & recovery consuming approvals and emitting `workspace.deleted` on parent symlink swap or move
- **Verification against code and docs:** `docs/plan.md` (§7.4, §10.7) requires parent-symlink containment validation before deleting workspaces and requires never consuming a `delete` approval or auditing deletion unless the workspace was legitimately removed. On `08708f2`, when `_delete_workspace_dir_sync` rejected a swapped/moved `tasks` parent symlink before `rmtree_allow_readonly` by raising `MeshError(INVALID_ARGUMENT)`, `run_workspace_delete`'s `except BaseException:` checked only `not any(os.path.isdir(p) or os.path.lexists(p) for p in check_paths)`, saw `False` through the new parent symlink, consumed the approval, and recorded a fake `workspace.deleted` event while the real directory remained untouched. `recovery._step_ws_reservations` (`kind == "delete"`) had the identical `not still_exists` flaw on boot recovery.
- **Root-cause fixes (`mesh/daemon/handlers/run.py`, `mesh/daemon/recovery.py`):**
  - Added `_validate_delete_target_ancestors(stripped, real, matched_root, project_id, task_id)` (checks non-symlink `tasks_dir` + `_verify_workspace_ancestors_not_symlinked` matching `real`) and an explicit `state: Optional[Dict[str, bool]] = None` tracker in `_delete_workspace_dir_sync` that sets `state["rmtree_started"] = True` only after ancestor validation succeeds and right before `rmtree_allow_readonly`.
  - Updated `run_workspace_delete`'s `except BaseException:` to require `del_state.get("rmtree_started") and ancestors_ok and not still_exists`, offloading best-effort `git worktree prune` via `await asyncio.to_thread` before opening `BEGIN IMMEDIATE;`. Any pre-rmtree failure or parent symlink swap leaves the approval `approved`, emits zero `workspace.deleted` events, clears only `ws_res:*`, and re-raises `MeshError`.
  - Updated `recovery._step_ws_reservations` (`kind == "delete"`): validates `ancestors_valid` (checks non-symlink `tasks_dir` and either `_verify_workspace_ancestors_not_symlinked` with `matched_root`/`expected_real` or `_match_task_workspace_with_roots` fallback when `matched_root` is missing) alongside `not still_exists`; when ancestors were tampered with, rolls back via `workspace.delete_rolled_back` (`recovered=True`) without touching the approval or idempotency cache.

---

## Related-Code Audit

Audited `mesh/daemon/merge.py`, `mesh/daemon/handlers/run.py`, `mesh/daemon/recovery.py`, `mesh/daemon/server.py`, `mesh/core/paths.py`, `mesh/core/gitops.py`, and `mesh/core/workspaces.py`:
- **Gitops allow-list & subprocess audit:** Verified via `git diff mesh/core/gitops.py` and `git grep subprocess mesh/` that `mesh/core/gitops.py` A6 `ALLOWED_SUBCOMMANDS` / `FORBIDDEN_SUBCOMMANDS` are completely unchanged and zero direct `git` subprocess calls were added outside `gitops.py`.
- **`workspace.prune` / `workspace.delete` async & TX boundaries:** Audited all `run_workspace_prune` (`TX1`, worker thread, `except BaseException`, `TX2`, `TX2 check_authz`), `run_workspace_delete` (`TX1`, `prepare_workspace_delete`, worker thread, `except BaseException`, `TX2`), and `server.py` dry-run/live dispatch branches: all blocking `git` and recursive filesystem operations run off the event loop and outside `BEGIN IMMEDIATE`. (TX1 in `run_workspace_delete` executes only single-stat `os.path.isdir(real)` plus local `lstat`/`realpath` ancestor validation, with no git calls.)
- **`docs/usage.md` lines 586–588:** Left untouched as in R1–R3 to preserve historical doc-test compatibility (`test_docs_m3.py`, `test_docs_m19.py`).

---

## Crash-Point Tables

### A. Merge Pipeline & Boot Recovery Crash Points (`mesh/daemon/merge.py`)

| Point / Window | State on Disk & Git | Boot Recovery / Handler Behavior | Task & Audit Outcome |
|---|---|---|---|
| Crash before `merge --no-ff --no-commit` (`pre_merge.json`, `pre_merge.index`, `pre_untracked.json`, `pre_empty_dirs.json` absent or written) | `integ` clean, `MERGE_HEAD` absent, `HEAD == pre_head` | `not _merge_head_present`, no `commit_ready`, if `pre_merge_*` exists runs `_abort_and_verify` (no-op clean); else clean verification | Task `merging -> merge_failed` (`interrupted`, `restored=True`), `merge:<prj>` released |
| Crash during tests after `test_command` ran `git commit` (even with `task_id` in message) or `git checkout -b`, before TX2 | `MERGE_HEAD` absent, `commit_ready.json` absent, `pre_merge.json` + `pre_merge.index` present, `HEAD` moved | `decide_recovery` sees snapshot present and `commit_ready is None` -> refuses reconcile -> `_abort_and_verify` restores `.git/HEAD` & `refs/heads/<integration_branch>` to `pre_head`, restores `pre_merge.index`, clears hide flags, restores tracked tree, cleans inside empty gitlinks & untracked | Task `merge_failed` (`interrupted`, `restored=True`, `head_moved=True`, `merge_commit_match=False`), rogue commit undone |
| Test runs `git merge --abort` + `git add` or `git update-index --skip-worktree`/`--assume-unchanged` and crashes (`after_merge_abort` or kill) | `MERGE_HEAD` absent, hide flags set and/or `.git/index` modified | With `pre_merge_*`: `_abort_and_verify` restores `pre_merge.index`, clears hide flags, restores tracked files from `true_pre`, cleans empty gitlinks & new untracked. Legacy `run_dir` without `pre_merge.*`: `_dirty_paths` detects hide flags & `decide_recovery` runs `_clear_index_hide_flags` before `_restore_tracked_from_ref` | Task `merge_failed` (`interrupted`, `restored=True`), no stuck hide bits for next merge |
| Test runs `git commit` then an unexpected exception occurs before `_post_test_dirty_paths` completes | `MERGE_HEAD` cleared by test's commit; `pre_untracked is not None` and `not committed` | `_run_merge_pipeline` `except` block enters `_abort_and_verify` (gated on `_merge_head_present or pre_untracked is not None`) -> resets branch ref, restores `pre_merge.index` & clean worktree | Returns `outcome="failed", reason="git_failed", restored=True`, integration clean |
| Crash right after `git commit --no-edit` (`MESH_TEST_CRASH_AT=after_merge_commit`) | `commit_ready.json` present matching `task_id`, `cr_pre`, `tip`, `symref`, and exact message | `decide_recovery` verifies `commit_ready.json` + `parents == [cr_pre, tip]` + exact title + `symref` -> reconciles `outcome="merged"` (`reconciled=True`) | Task `merging -> merged`, idempotent on reboot |
| Crash with spoofed merge commit contradicting `commit_ready.json` (wrong title, wrong symref, or wrong parents) | `commit_ready.json` present, `HEAD` mismatch | `reconcile_ok = False` -> routes to `_abort_and_verify` with `pre_merge.json` / `pre_merge.index` -> rolls back to `pre_head` | Task `merge_failed` (`interrupted`, `restored=True`), spoofed commit rejected |
| Symlink replacing empty tracked `160000` gitlink (e.g. `vendor_sub -> .`) or artifacts leaked inside empty gitlink (`vendor_sub/leaked.txt`) | Safe gitlinks only enter `skip_paths_real`; empty-snapshot gitlinks classified `cleanable` | Tampered gitlink excluded from `skip_paths_real` (no `chmod 0o000` bypass), symlink unlinked & empty dir restored; leaked files inside `cleanable` gitlinks wiped while populated submodules stay intact | Failing test -> `tests_failed`/`interrupted` (`restored=True`); passing test with empty-gitlink leak cleans dir -> `merged` cleanly |

### B. Workspace Delete & Prune Crash Points (`mesh/daemon/handlers/run.py`, `mesh/daemon/recovery.py`)

| Point / Window | Reservation & Checkpoint State | Recovery / Handler Behavior | Approval, Audit & Idempotency |
|---|---|---|---|
| `run_workspace_delete` Phase 2 `BaseException`/`MeshError` before `rmtree_allow_readonly` (parent swapped to symlink or moved) | `ws_res:task:<id>` present; `del_state["rmtree_started"]` is `False` (and `ancestors_ok` is `False`) | `confirmed_deleted = False`; skips `worktree prune`, does NOT consume approval, records zero `workspace.deleted` events, clears `ws_res:*`, re-raises error | Approval remains `approved`; original dir in moved parent untouched; retry after fixing parent deletes cleanly |
| `run_workspace_delete` Phase 2 `BaseException` after `rmtree_allow_readonly` removed dir | `del_state["rmtree_started"]` is `True`, `ancestors_ok` is `True`, `still_exists` is `False` | Offloads `git worktree prune` via `asyncio.to_thread`, then in `BEGIN IMMEDIATE` consumes pinned `approved` approval, records `system` `workspace.deleted`, deletes `ws_res:*`, re-raises | Approval `consumed`; audit reflects real physical deletion |
| `after_ws_delete` recovery when parent `tasks` dir was swapped for symlink or moved away | `ws_res:task:<id>` present; `ancestors_valid` is `False` | `_step_ws_reservations` sees `not (ancestors_valid and not still_exists)` -> emits `workspace.delete_rolled_back` (`recovered=True`), deletes `ws_res:*` | Approval stays `approved`; zero `workspace.deleted` events; zero idempotency row stored |
| `after_ws_prune_reserve` (crash before worker filters `active_planned_ids`) | Ckpt `{active_planned_ids: None, completed: False}` | `reconcile_prune_checkpoint_from_data` returns `removed=[], completed=False` without running `git` on historical tasks -> rolls back | Zero git probes for historical tasks; zero duplicate `workspace.pruned` events |
| `run_workspace_prune` Phase 2 `BaseException` | Ckpt has `active_planned_ids` & `in_flight` | Handler reads SQLite meta before TX, runs `reconcile_prune_checkpoint_from_data` in `asyncio.to_thread` outside TX, then `BEGIN IMMEDIATE` emits deduplicated `workspace.pruned` (`(task_id, request_id)`) & clears `ws_res:*` | Zero blocking git on event loop or in TX; historical tasks unaffected |
| `workspace.prune` when `integ_wt` missing and `task["branch"]` non-empty | Worktree dir present or absent; no valid integration worktree | `_remove_worktree_and_branch_with_roots` returns `"branch_delete_failed"` **before** removing `real_path` | Worktree untouched; reported in `skipped`; zero false `workspace.pruned` events |

---

## Modified and Added Files

- **Production (`mesh/`):**
  - `mesh/daemon/merge.py`
  - `mesh/daemon/handlers/run.py`
  - `mesh/daemon/server.py`
  - `mesh/daemon/recovery.py`
- **Tests (`tests/`):**
  - `tests/unit/test_batch7_pr45_r3.py` (added `restored`, `head`, `has_merge_head`, `is_clean` assertions at lines 255–258)
  - `tests/unit/test_batch7_pr45_r4.py` (new, 13 unit tests)
  - `tests/integration/test_batch7_pr45_r4.py` (new, 3 end-to-end daemon/subprocess integration tests)
- **Documentation (`docs/`):**
  - `docs/impl-notes-batch7-pr45-r4.md`

---

## Verification

The full test runner (`scripts/test.sh`) was intentionally not run per task rules. Every relevant new and existing regression batch passed after the final code edit:

- **Failing-first baseline on `cloud/pr45r4-base` (`08708f2`)** (isolated `--shared` clone `/tmp/base-r4-check`, removed afterwards):
  - `python3 -m unittest tests.unit.test_batch7_pr45_r4 tests.unit.test_batch7_pr45_r3 tests.integration.test_batch7_pr45_r4`
  - Result: `Ran 31 tests in 19.351s — FAILED (failures=15, errors=7)` (failed/errored across all 13 unit tests in `tests/unit/test_batch7_pr45_r4.py`, all 3 E2E integration tests in `tests/integration/test_batch7_pr45_r4.py`, and the tightened assertions in `tests/unit/test_batch7_pr45_r3.py:255-258`, while the 14 unchanged R3 tests passed).
- **3× repeated PR #45 R4 + R3 suite on `pr45-r4`:**
  - `python3 -m unittest tests.unit.test_batch7_pr45_r4 tests.unit.test_batch7_pr45_r3 tests.integration.test_batch7_pr45_r4`
  - Run 1: `Ran 31 tests in 28.231s — OK`
  - Run 2: `Ran 31 tests in 28.097s — OK`
  - Run 3: `Ran 31 tests in 27.697s — OK`
- **Related unit & recovery suite (23 modules):**
  - `python3 -m unittest tests.recovery.test_recovery tests.recovery.test_recovery_adopt_seen tests.recovery.test_recovery_lead_merge tests.recovery.test_recovery_merge tests.unit.test_batch7_pr45_r4 tests.unit.test_batch7_pr45_r3 tests.unit.test_batch7_pr45_r2 tests.unit.test_batch7_pr45_r1 tests.unit.test_batch7_g4 tests.unit.test_batch7_pr43_r1 tests.unit.test_batch7_pr43_r2 tests.unit.test_batch7_pr43_r3 tests.unit.test_merge tests.unit.test_merge_recovery_minors tests.unit.test_recovery_units tests.unit.test_gitops tests.unit.test_paths tests.unit.test_docs_m3 tests.unit.test_docs_m19 tests.unit.test_invariants tests.unit.test_repos tests.unit.test_projects tests.unit.test_states`
  - Result: `Ran 232 tests in 144.467s — OK` (exit 0).
- **Related integration suite (14 modules):**
  - `python3 -m unittest tests.integration.test_batch7_pr45_r4 tests.integration.test_batch7_pr45_r3 tests.integration.test_batch7_pr45_r2 tests.integration.test_batch7_pr45_r1 tests.integration.test_batch7_g4 tests.integration.test_deliver_prune tests.integration.test_merge tests.integration.test_merge_tests tests.integration.test_code_tasks tests.integration.test_refs_worktrees tests.integration.test_approvals tests.integration.test_delegated_approval tests.integration.test_daemon tests.integration.test_merge_refs`
  - Result: `Ran 123 tests in 296.822s — OK` (exit 0).
- **Static & constraint checks:**
  - `python3 -m py_compile mesh/daemon/merge.py mesh/daemon/handlers/run.py mesh/daemon/server.py mesh/daemon/recovery.py tests/unit/test_batch7_pr45_r3.py tests/unit/test_batch7_pr45_r4.py tests/integration/test_batch7_pr45_r4.py` — exit 0
  - `git diff --check` — exit 0
  - `git diff docs/usage.md mesh/core/gitops.py` — empty (`mesh/core/gitops.py` A6 allow-list and `docs/usage.md` untouched)
  - `git grep subprocess mesh/` — zero new `subprocess` imports or direct `git` calls outside `mesh/core/gitops.py`
