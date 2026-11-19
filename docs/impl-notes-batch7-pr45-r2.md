# Batch 7 PR #45 Review R2 Implementation Notes

This note records verification against the codebase and documentation (`docs/requirements.md`, `docs/usage.md`, `docs/plan.md`), root-cause fixes, design tradeoffs, and regression verification for all four findings in the PR #45 round-2 review (`pr45r2-review.md`) on branch `pr45-r2` (based on `cloud/pr45-base`, PR #45 r1 commit `da3f281`).

All four findings (P1-1, P1-2, P2, P3) were confirmed real against the codebase and covered by regression tests in `tests/unit/test_batch7_pr45_r2.py` (12 tests) and `tests/integration/test_batch7_pr45_r2.py` (4 end-to-end daemon/subprocess tests, including `MESH_TEST_CRASH_AT` crash recovery).

## Verified and Fixed Findings

1. **P1-1 (`workspace.delete` and `workspace.prune` running irreversible filesystem/git deletions prior to `BEGIN IMMEDIATE`, approval consumption, lock/lease exclusion, and TX1 idempotency checks):** Confirmed real.
   - **Verification against code and docs:** `docs/plan.md` (§7.4, §10.7, §13.4) and `docs/usage.md` (§8) require agent `workspace.delete` to execute only after approval validation and consumption (and keep the branch), `workspace.prune` to skip leased/merging tasks cleanly and never prune unmerged work, idempotent replays to avoid duplicate side effects, and audit events to reflect real filesystem state. On `da3f281`, `DaemonServer.handle_client` executed `prepare_workspace_delete` and `execute_workspace_prune_plan` before `BEGIN IMMEDIATE`, allowing mid-flight approval cancellation/expiry/replay, concurrent `task.start` / `task.merge` / `workspace.delete` races, lead-supersede bypass in TX2 `handle_workspace_prune`, and missing audit events if TX rolled back.
   - **Design & fixes implemented:**
     - Chose `meta` reservation rows (`ws_res:task:<task_id>`, `ws_res:prune_proj:<project_id>`, `ws_res:prune_all`) rather than synthetic `leases` rows on `accepted`/`queued`/`merged`/`cancelled` tasks so database invariants I1 (`Lease task:<id> held for task in illegal status`) and I8 (`No lease exists without an active run`) in `mesh/core/invariants.py` hold at every crash point.
     - `mesh/core/workspaces.py`: Added `ws_res:*` reservation helpers (`ws_delete_reservation_key`, `ws_prune_project_reservation_key`, `load_ws_reservations`, `has_task_ws_reservation`, `has_project_any_ws_reservation`, `list_project_ws_reservation_keys`, `pinned_approval_ids`, `is_approval_pinned_by_ws_reservation`, `find_ws_reserved_idempotency`).
     - `mesh/daemon/handlers/run.py`:
       - Added 3-phase `run_workspace_delete(server, ctx)`:
         - **TX1 (`BEGIN IMMEDIATE`):** re-checks idempotency (`_check_tx1_idempotency`), runs `_validate_workspace_delete(consume_approval=False, create_approval_if_missing=True)` (committing pending approvals on exit 7 `APPROVAL_REQUIRED`), fast-paths in TX1 when the directory is already absent (`deleted: False`), or inserts `ws_res:task:<task_id>` with the pinned `approval_id` and idempotency metadata.
         - **Phase 2 (off-TX worker):** runs `asyncio.to_thread` wrapped in `asyncio.shield` and registered via `server.track_merge_job` under `_task_lock(task_id)`, calling `_delete_workspace_dir_sync`. Supports testing-only `MESH_TEST_CRASH_AT` hooks `after_ws_reserve` and `after_ws_delete`. On `BaseException`, clears the reservation without consuming the approval or storing idempotency so the operation can be retried cleanly.
         - **TX2 (`BEGIN IMMEDIATE`):** re-checks `check_authz` (if superseded mid-flight after disk deletion already succeeded, consumes the pinned approval if valid, emits a `system` `workspace.deleted` audit event, clears `ws_res:*`, commits, then re-raises the authz error), consumes the pinned approval (`allow_expired_if_reserved=True`), records `workspace.deleted`, deletes `ws_res:*`, stores idempotency via `_store_tx_idempotency`, and commits.
       - Added 3-phase `run_workspace_prune(server, ctx)`:
         - **TX1 (`BEGIN IMMEDIATE`):** `_check_tx1_idempotency`, `plan_workspace_prune(ctx)` (validates project existence, authz, active `ws_res:*` exclusion, and snapshots active `task:*`/`merge:*` leases), writes initial checkpoint `$MESH_HOME/run/ws_prune_<hex>.json`, inserts `ws_res:prune_proj:<pid>` or `ws_res:prune_all`, and commits.
         - **Phase 2 (off-TX shielded worker):** runs `execute_workspace_prune_plan` under per-task `_task_lock`, updating the checkpoint atomically after each item and writing `completed: True` on finish. Supports `MESH_TEST_CRASH_AT` hooks `after_ws_prune_reserve` and `after_ws_prune`. On `BaseException`, records `system` `workspace.pruned` events for any items already checkpointed as removed, clears `ws_res:*`, and unlinks the checkpoint without storing idempotency.
         - **TX2 (`BEGIN IMMEDIATE`):** re-checks `check_authz` (if caller lost lead authority mid-flight, records `system` `workspace.pruned` events for already-removed worktrees/branches and clears `ws_res:*` before re-raising), records `workspace.pruned` events, clears `ws_res:*`, stores idempotency, commits, and unlinks the checkpoint.
     - `mesh/daemon/server.py`:
       - Unified `workspace.delete` and `workspace.prune` (when `not dry_run`) into the long-request dispatch branch alongside `task.merge`, preserving exactly two `run_scheduler_tick(self.db_conn` call sites after `_scheduler_loop` (`test_no_scheduler_tick_while_stopping`).
       - Checked `find_ws_reserved_idempotency` both pre-TX and inside `BEGIN IMMEDIATE`, returning `CONCURRENCY_LIMIT` (rc 11) when an identical idempotency key is in flight and `IDEMPOTENCY_KEY_REUSED` (rc 6) on parameter mismatch.
       - Guarded `ROLLBACK`/`COMMIT` error paths with `self.db_conn.in_transaction`.
     - `mesh/core/approvals.py`, `mesh/daemon/handlers/approval.py`, `mesh/daemon/scheduler.py`: Blocked `approval.{approve,reject,cancel,consume}` with `CONCURRENCY_LIMIT` while pinned by `ws_res:*`; prevented `sweep_expired_approvals` and `create_or_get_pending_approval` from expiring pinned approvals; added `allow_expired_if_reserved=True` in TX2 consumption so a pinned approval cannot fail due to clock advance or policy tightening during Phase 2.
     - `mesh/daemon/merge.py`, `mesh/daemon/supervisor.py`, `mesh/daemon/handlers/task.py`, `mesh/daemon/handlers/project.py`: Added `has_task_ws_reservation` / `has_project_any_ws_reservation` / `list_project_ws_reservation_keys` exclusions in `check_merge_request`, `_spawn_run`, `_spawn_one`, `prepare_task_start_workspace`, `task.{start,cancel,close,reassign,retry}`, and `project.{set_lead,cancel,complete}`.
     - `mesh/daemon/recovery.py`: Added `_step_ws_reservations(conn, home, counts)` before `sweep_expired_approvals`:
       - Leftover `delete` reservations: if the workspace directory is still on disk, emits `workspace.delete_rolled_back` (`recovered: True`) and removes `ws_res:*` (keeping the approval intact and omitting idempotency so retries proceed); if the workspace directory is gone, runs best-effort `git worktree prune`, marks any pinned approval `'consumed'` with `consumed_at`/`consumed_by`/`consumed_request_id`, records `system` `workspace.deleted` (`recovered: True`), stores the idempotency row (`ignore_conflict=True`), and deletes `ws_res:*`.
       - Leftover `prune` reservations: reads the prune checkpoint, emits `system` `workspace.pruned` (`recovered: True`) for every removed entry recorded before the crash, and either finalizes idempotency (`completed=True`, incrementing `workspace_prunes_finalized`) or emits `workspace.prune_rolled_back` (`completed=False`, incrementing `workspace_prunes_rolled_back`) before clearing `ws_res:*` and unlinking the checkpoint.

2. **P1-2 (Merge pipeline and crash recovery wedged when `test_command` edits/deletes tracked files or creates `0o000`/`0o100`/empty untracked directories):** Confirmed real.
   - **Verification against code and docs:** `docs/plan.md` (§10.5 step 2 & A6 allow-list, §11.7 step 5) requires a failed/aborted merge or crash recovery to restore the integration worktree cleanly (`HEAD == pre_head`, clean status, no `MERGE_HEAD`) without using forbidden git subcommands (`reset --hard`, `checkout -f`, `clean -f`, `--force`). On `da3f281`:
     1. When `test_command` edited a tracked file involved in the merge, `git merge --abort` exited 128 and left `MERGE_HEAD` present; when it edited/deleted an uninvolved tracked file, `git merge --abort` succeeded but left unstaged tracked edits dirty, wedging subsequent merges with `INTEGRATION_DIRTY`.
     2. `git status --porcelain=v1 --untracked-files=all -z` silently omitted `0o000`/`0o100` directories (only warning on stderr), and empty directories were not tracked or cleaned.
   - **Note on two existing references:**
     - `tests/unit/test_batch7_pr45_r1.py:292-301` (`stubborn.tmp`) intentionally mocks `merge._safe_unlink_untracked` with `side_effect=OSError("cant_unlink")`, which also blocks `_abort_and_verify` cleanup (`details["restored"]` remains `False` under that fault-injection mock). Non-mocked `restored is True` checks for tracked-file modifications/deletions, `0o000`/`0o100` dirs, and empty dirs were added in `tests/unit/test_batch7_pr45_r2.py` and `tests/integration/test_batch7_pr45_r2.py`.
     - `docs/usage.md:586-588` still carries pre-PR45 wording ("Test commands must leave no untracked, non-ignored artifacts... Mesh does not clean those files for you"); kept unchanged as `docs/plan.md` §10.5 and PR #45 requirements govern automatic post-test cleanup/abort restoration.
   - **Fixes implemented:**
     - `mesh/core/paths.py`: Added `skip_names: Optional[Set[str]] = None` to `make_owned_dirs_writable` so callers can pass `skip_names={".git"}`.
     - `mesh/daemon/merge.py`:
       - Added `_snapshot_empty_dirs`, `_save_pre_empty_dirs` (`pre_empty_dirs.json` in `run_dir`), `_load_pre_empty_dirs`, and `_cleanup_empty_dirs`; called bounded `make_owned_dirs_writable(integ, boundary_real=..., skip_names={".git"})` before `_untracked_paths` and `_cleanup_empty_dirs` in `_cleanup_new_untracked`, so `0o000`/`0o100`/empty directories become traversable, detected, and deleted safely; extended `_post_test_dirty_paths`, `_abort_and_verify`, and `decide_recovery` with `pre_empty_dirs`.
       - Added `_parse_diff_raw_z`, `_restore_single_tracked_path`, and `_restore_tracked_from_ref` using only A6 allow-listed `git diff --raw --no-renames -z`, `git cat-file blob`, and `git status --porcelain=v1`. When `git merge --abort` fails with `MERGE_HEAD` present, restores tracked worktree paths from index `:0:` and retries `git merge --abort`; once `MERGE_HEAD` is cleared, restores any remaining dirty tracked paths (edits, deletions, type swaps to symlinks/dirs) from `pre_head` and cleans leftover untracked/empty dirs.

3. **P2 (`validate_setting("workspaces.root")` permitting symlinked ancestors and persisting non-canonical paths):** Confirmed real.
   - **Verification against code and docs:** `validate_setting` in `mesh/core/settings.py` only checked `os.path.islink(val_clean)` on the leaf directory and returned the raw `val` string instead of `val_real`, while `_verify_workspace_ancestors_not_symlinked` in `mesh/daemon/handlers/run.py` stopped walking ancestors at `matched_root_clean` and `config.py` passed raw paths to `remember_protected_path`. Repointing an upper-level symlink could therefore redirect runtime deletions to arbitrary matching directories outside the physical root or defeat repo overlap checks.
   - **Fixes implemented:**
     - `mesh/core/settings.py`: Walks every ancestor of `os.path.abspath(val_clean)` up to `/` in `validate_setting("workspaces.root")`, raising `INVALID_SETTING` (rc 2) if any component is a symlink, and returns `val_real = os.path.realpath(val)` for canonical persistence.
     - `mesh/daemon/handlers/config.py`: Resolves both old and new `workspaces.root` with `os.path.realpath(...)` when calling `remember_protected_path`.
     - `mesh/daemon/handlers/run.py`: Added `_has_symlink_ancestor` and invoked it for both `stripped_clean` and `matched_root_clean` inside `_verify_workspace_ancestors_not_symlinked` so even legacy DB entries with symlinked ancestors are rejected at delete/prune time without touching files.

4. **P3 (`handle_run_release`, `DaemonServer._drain_for_shutdown`, and `_fail_lead_turn_spawn` missing `testing_mode` forwarding):** Confirmed real.
   - **Verification against code and docs:** `docs/plan.md` (§12.4 lead-turn backoff, §17.1 testing mode isolation) requires daemon components to honor the daemon's `testing_mode` rather than falling back to `os.environ.get("MESH_TESTING")`. Confirmed real in all three cited locations (other `_close_run`, `_release_messages`, and `retry_spawn_aborts` call sites were also audited and confirmed covered).
   - **Fixes implemented:**
     - `mesh/daemon/handlers/run.py`: Passed `testing_mode=ctx.testing_mode` to `supervisor._close_run` in `handle_run_release`.
     - `mesh/daemon/server.py`: Passed `testing_mode=self.testing_mode` to `supervisor.retry_spawn_aborts` in `DaemonServer._drain_for_shutdown`.
     - `mesh/daemon/supervisor.py` & `mesh/daemon/lead_turns.py`: Plumbed `testing_mode: Optional[bool] = None` through `_SpawnAbort`, `_close_spawn_abort_db`, `retry_spawn_aborts`, `handle_spawn_failure`, `_spawn_run`, `commit_lead_turn`, and `_fail_lead_turn_spawn` so `_release_messages` and lease-renewal TTL clamping always use the explicit `testing_mode`.

## Modified and Added Files

- Production code (`mesh/`): `mesh/core/approvals.py`, `mesh/core/paths.py`, `mesh/core/settings.py`, `mesh/core/workspaces.py`, `mesh/daemon/handlers/approval.py`, `mesh/daemon/handlers/config.py`, `mesh/daemon/handlers/project.py`, `mesh/daemon/handlers/run.py`, `mesh/daemon/handlers/task.py`, `mesh/daemon/lead_turns.py`, `mesh/daemon/merge.py`, `mesh/daemon/recovery.py`, `mesh/daemon/scheduler.py`, `mesh/daemon/server.py`, `mesh/daemon/supervisor.py`.
- Regression tests: `tests/unit/test_batch7_pr45_r2.py`, `tests/integration/test_batch7_pr45_r2.py`.
- Implementation notes: `docs/impl-notes-batch7-pr45-r2.md`.

## Verification

All checks below ran cleanly after the final code edit (`scripts/test.sh` was intentionally not run per task instructions):

- **Repeated PR #45 r2 regression runs** (`python3 -m unittest tests.unit.test_batch7_pr45_r2 tests.integration.test_batch7_pr45_r2`):
  - Run 1: **16 tests in 13.930s — OK**
  - Run 2: **16 tests in 14.391s — OK**
  - Run 3: **16 tests in 14.627s — OK**
- **Targeted core unit + recovery + PR #45 r2 suite** (`python3 -m unittest tests.unit.test_batch7_pr45_r2 tests.integration.test_batch7_pr45_r2 tests.unit.test_batch7_pr45_r1 tests.unit.test_batch7_g4 tests.unit.test_batch6_shutdown tests.unit.test_merge tests.unit.test_merge_recovery_minors tests.unit.test_settings tests.unit.test_paths tests.unit.test_invariants tests.unit.test_recovery_units tests.unit.test_lead_turns tests.unit.test_scheduler tests.unit.test_skeleton tests.recovery.test_recovery tests.recovery.test_recovery_adopt_seen tests.recovery.test_recovery_lead_merge tests.recovery.test_recovery_merge`):
  - **197 tests in 144.954s — OK**
- **Expanded 44-module unit + recovery regression batch** (workspaces, merge, recovery, settings, shutdown, invariants, stdlib-only skeleton, doc/linear checks, and all 4 `tests.recovery.*` modules):
  - **581 tests in 266.569s — OK**
- **Integration batch 1 (13 modules)** (`test_batch7_pr45_r1`, `test_batch7_pr45_r2`, `test_batch7_g4`, `test_deliver_prune`, `test_approvals`, `test_delegated_approval`, `test_merge`, `test_merge_tests`, `test_repo_projects`, `test_refs_worktrees`, `test_code_tasks`, `test_daemon`, `test_setup`):
  - **121 tests in 285.309s — OK**
- **Integration batch 2 (8 modules)** (`python3 -m unittest tests.integration.test_batch7_pr43_r1 tests.integration.test_batch7_pr43_r2 tests.integration.test_batch7_pr44_r1 tests.integration.test_batch7_g2 tests.integration.test_batch7_g3 tests.integration.test_run_release_seen tests.integration.test_projects tests.integration.test_lead_turns`):
  - **45 tests in 184.229s — OK**
- **Integration batch 3a (4 modules)** (`python3 -m unittest tests.integration.test_execution_termination tests.integration.test_execution_launch tests.integration.test_execution_launch_fences tests.integration.test_execution_failure_policy`):
  - **35 tests in 198.006s — OK**
- **Integration batch 3b (4 modules)** (`python3 -m unittest tests.integration.test_execution_inbox tests.integration.test_prepublic_p2 tests.integration.test_linear_full_core tests.integration.test_linear_full_daemon`):
  - **41 tests in 87.723s — OK**
- **Hygiene & bytecode compile checks**: `git diff --check` and `python3 -m py_compile` across all 15 modified `mesh/` files and both new test files exited `0`.
