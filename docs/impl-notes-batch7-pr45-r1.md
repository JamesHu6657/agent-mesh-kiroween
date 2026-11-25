# Batch 7 PR #45 Review R1 Implementation Notes

This note records verification results and implementation changes for the six review findings in `~/b7/pr45-review.md` on branch `pr45-r1`.

All six findings (P1-1, P1-2, P2-1, P2-2, P2-3, P3-1) were confirmed real against the codebase and covered with failing regression tests in [tests/unit/test_batch7_pr45_r1.py](../tests/unit/test_batch7_pr45_r1.py) and [tests/integration/test_batch7_pr45_r1.py](../tests/integration/test_batch7_pr45_r1.py) before applying fixes.

## Verified and Fixed Findings

1. **P1-1 (Historical `workspaces.root` values and symlinked default workspaces root bypassing overlap checks):** Confirmed real.
   - [mesh/core/workspaces.py](../mesh/core/workspaces.py): Extracted `allowed_workspace_roots(conn, home)` returning the current `workspaces.root`, the default `<home>/workspaces`, and all persisted `meta` entries prefixed with `protected_path:` (`PROTECTED_PATH_META_PREFIX`).
   - [mesh/daemon/handlers/project.py](../mesh/daemon/handlers/project.py): Updated `_prepare_repo_project` to check bidirectional `is_path_equal_or_parent` against every entry returned by `allowed_workspace_roots(ctx.conn, ctx.home)`.
   - [mesh/core/settings.py](../mesh/core/settings.py): Updated `validate_setting` (`workspaces.root`) to use `os.path.join(home_real, "workspaces")` without resolving symlinks before checking, and reject symlinked `val` or symlinked default `<home>/workspaces` directories.
   - [mesh/daemon/handlers/run.py](../mesh/daemon/handlers/run.py): Reused `allowed_workspace_roots` via `_allowed_workspace_roots` and added symlink checks on `root_dir` in `_verify_workspace_ancestors_not_symlinked` / `_task_workspace_match`.

2. **P1-2 (Merge crash recovery skipping untracked cleanup and online cleanup failing on read-only leftovers):** Confirmed real.
   - [mesh/core/paths.py](../mesh/core/paths.py): Added `make_owned_dirs_writable`, `make_owned_ancestors_writable`, and `rmtree_allow_readonly` to retry after `PermissionError` by adding owner write/search (`0o700`) permissions only on owned non-symlink directories within the configured boundary.
   - [mesh/daemon/merge.py](../mesh/daemon/merge.py):
     - Updated `_safe_unlink_untracked` to retry with `make_owned_ancestors_writable` and `rmtree_allow_readonly(..., boundary_real=integ_real)`.
     - Persisted pre-test untracked paths to `pre_untracked.json` (`_save_pre_untracked`, `_load_pre_untracked`) in `run_dir` before running `run_test_step`.
     - Added `_post_test_dirty_paths(integ, pre_untracked)` checking unexpected untracked entries and unstaged worktree changes (without flagging staged `MERGE_HEAD` index entries) and aborted with `_abort_and_verify(..., reason="integration_dirty")` when post-test cleanup leaves the integration worktree dirty before `git commit`.
     - Updated `decide_recovery` and `recover_merging_task` so `MERGE_HEAD` crash recovery loads `pre_untracked.json` from `run_dir` (defaulting to `set()`) and passes `pre_untracked` to `_abort_and_verify`.

3. **P2-1 (`workspace delete` error swallowing after commit, parent symlink TOCTOU, and synchronous `workspace prune`):** Confirmed real.
   - [mesh/core/approvals.py](../mesh/core/approvals.py): Added `consume: bool = True` to `check_and_consume_approval` so pre-transaction validation performs full approval verification without mutating SQLite.
   - [mesh/daemon/handlers/run.py](../mesh/daemon/handlers/run.py):
     - Extracted `_verify_workspace_ancestors_not_symlinked` and `_delete_workspace_dir_sync` (re-verifying `matched_root`, `<project_id>`, `tasks`, and target leaf non-symlink status + `realpath` containment immediately before `rmtree_allow_readonly`, followed by best-effort `git worktree prune`).
     - Added `async def prepare_workspace_delete(ctx)` to validate request parameters, authz, channel/approval (`consume=False`, deferring `APPROVAL_REQUIRED` creation to TX), leases, and workspace path before offloading directory removal to `asyncio.to_thread(_delete_workspace_dir_sync, ...)` outside `BEGIN IMMEDIATE`.
     - Updated `handle_workspace_delete` to consume `ctx.prepared_delete` (or run `_delete_workspace_dir_sync` synchronously prior to `record_event` in direct unit calls) rather than emitting a post-commit `delete_workspace` effect, preventing `workspace.deleted` audit events or `deleted: true` responses when filesystem deletion fails.
     - Split `prepare_workspace_prune` into SQLite-only `plan_workspace_prune(ctx)` and thread-safe `execute_workspace_prune_plan(plan)`.
   - [mesh/daemon/server.py](../mesh/daemon/server.py): Updated `handle_client` to await `run_handlers.prepare_workspace_delete(ctx)` and offload `run_handlers.execute_workspace_prune_plan` via `asyncio.to_thread` prior to `BEGIN IMMEDIATE`, and hardened the fallback `delete_workspace` effect handler in `execute_effects` with parent symlink checks and `rmtree_allow_readonly`.
   - [tests/unit/test_batch7_g4.py](../tests/unit/test_batch7_g4.py): Updated `test_f5_prune_merge_lease_non_tx_delete_and_dry_run_plans` from PR #45's incompatible `Effect("delete_workspace")` expectation (`record_event` running before `shutil.rmtree`) to verify off-TX `handle_client` deletion, disk preservation when validation fails, and non-commit / zero `workspace.deleted` audit events on deletion `OSError`.

4. **P2-2 (`_termination_loop` not propagating `server.testing_mode`):** Confirmed real.
   - [mesh/daemon/server.py](../mesh/daemon/server.py): `_termination_loop` now propagates `testing_mode=tmode` to `advance_terminations` whenever `getattr(self, "testing_mode", None)` is not `None`.
   - [mesh/daemon/supervisor.py](../mesh/daemon/supervisor.py), [mesh/daemon/lead_turns.py](../mesh/daemon/lead_turns.py), [mesh/daemon/recovery.py](../mesh/daemon/recovery.py): Plumbed `testing_mode: Optional[bool] = None` through `advance_terminations`, `_advance`, `_finalize_termination`, `_finalize_lead_turn_termination`, `confirm_pending_exits`, `watch_launch_runs`, `_watch_one`, `_watch_one_inner`, `_watch_lead_turn`, `_watch_task_run`, `termination_reason`, `_adapter_liveness`, `parse_run_output`, `_task_auto_submit`, `_finish_run`, `_close_run`, `_needs_submit_git`, `retry_spawn_aborts`, `lead_turns.finish_lead_turn`, and `recovery.{recover,_step_runs,retry_deferred,_recover_run,_lost,_close_without_process}`, preserving positional call signatures when `testing_mode is None` for compatibility with existing test mocks.

5. **P2-3 (`approval check` marking terminal approvals as `APPROVAL_EXPIRED` past `expires_at`):** Confirmed real.
   - [mesh/daemon/handlers/approval.py](../mesh/daemon/handlers/approval.py): Updated `handle_approval_check` so `row["expires_at"] < now_iso_utc()` raises `APPROVAL_EXPIRED` only for `row["status"] in ("pending", "approved")`, returning terminal `rejected`, `cancelled`, and `consumed` rows cleanly.

6. **P3-1 (`project.update` ignoring plural `criteria` and vacuous whitespace tests):** Confirmed real.
   - [mesh/daemon/handlers/project.py](../mesh/daemon/handlers/project.py): Updated `handle_project_update` to accept `criteria` as well as `criterion`, reject invalid or whitespace-only `criteria` entries instead of silently dropping them, and return clear `INVALID_ARGUMENT` errors.
   - [tests/unit/test_batch7_g4.py](../tests/unit/test_batch7_g4.py) & [tests/integration/test_batch7_g4.py](../tests/integration/test_batch7_g4.py): Replaced the vacuous `--title` / `title` assertions with real whitespace validation assertions on `goal`, `criterion`, and `criteria` checking exact messages/error codes.
