# Batch 7 PR #45 Review R3 Implementation Notes

This note records verification against the codebase, the round-3 review (`review.md`), and project documentation (`docs/requirements.md` §5–§8, `docs/usage.md` §8, `docs/plan.md` §10.5, §10.7, §11.7, §13.4, §17.1), plus root-cause fixes, related-code audit findings, crash-point state tables, and test results on branch `pr45-r3` (based on `cloud/pr45r3-base` commit `7f95410`).

All four round-3 findings (`P1-1`, `P1-2`, `P2-1`, `P2-2`) were confirmed real against `7f95410` and covered by failing-first regression tests in `tests/unit/test_batch7_pr45_r3.py` (15 tests) and `tests/integration/test_batch7_pr45_r3.py` (4 end-to-end daemon/subprocess tests). Stashing the fixes against `7f95410` produced **14/15 failures** in the new suite (with only the intentional positive control `test_task_adds_new_gitlink_and_passing_tests_preserves_gitlink` passing).

---

## Verified and Fixed Findings

### 1. P1-1 — Post-test dirty check & pre-commit guard ignoring staged index changes and rogue commits
- **Verification against code and docs:** `docs/requirements.md` (§5) and `docs/plan.md` (§10.5) require that only reviewed task-branch changes enter `integration_branch` after tests pass cleanly. On `7f95410`, `_post_test_dirty_paths` (`mesh/daemon/merge.py`) inspected only untracked (`??`) entries and the worktree column (`code[1] != " "`) of `git status --porcelain=v1 --untracked-files=all -z`, completely ignoring the index column (`code[0]`). Because `run_test_step` executes inside `integ` while `--no-commit` merge changes are staged, any test exiting `0` after `git add`, `git rm --cached`, `git update-index --chmod=...`, modify + `git add`, or `git commit` produced an empty `_post_test_dirty_paths` list (except rogue commits failing `--no-edit` only in some flows), leaking unreviewed test mutations straight into the merge commit.
- **Root-cause fixes (`mesh/daemon/merge.py`):**
  - Added `_parse_diff_raw_z_full` and `_staged_index_snapshot` using strictly §10.5 allow-listed `git diff --cached --raw --full-index --no-renames -z HEAD` to snapshot `(old_mode, new_mode, old_oid, new_oid, status)` per path immediately before `run_test_step` (`pre_test_staged`).
  - Extended `_post_test_dirty_paths(integ, pre_untracked, pre_empty_dirs, pre_staged=..., pre_head=...)` to flag:
    1. Any staged-index delta (`cur_staged.get(rel) != pre_staged.get(rel)`), catching `git add`, `git rm --cached`, content re-staging on already-staged merge entries, and index mode changes.
    2. Any `HEAD != pre_head` movement caused by a rogue commit during tests (enumerating changed paths via `git diff --raw --no-renames -z <pre_head> <post_head>` or `.git/HEAD`).
  - Added a defense-in-depth pre-commit guard in `_run_merge_pipeline` right before `git commit --no-edit`: even if `_post_test_dirty_paths` were bypassed, it verifies `HEAD == pre_head`, `MERGE_HEAD` is still present, and `_staged_index_snapshot` matches `pre_test_staged`; any mismatch aborts via `_abort_and_verify` and returns `outcome="failed", reason="integration_dirty"`.
  - Audited `mesh/core/gitops.py`: all git invocations (`diff --cached --raw --full-index --no-renames -z HEAD`, `diff --raw --no-renames -z ...`, `rev-parse`, `status`) already comply with `ALLOWED_SUBCOMMANDS`; no allow-list widening was needed.

### 2. P1-2 — `_cleanup_empty_dirs` deleting tracked `160000` gitlink/submodule directories
- **Verification against code and docs:** In uninitialized worktrees (standard for Mesh `integration` worktrees), tracked `160000` gitlinks exist on disk as empty directories. On `7f95410`, three coupled bugs deleted these tracked directories and left `D <submodule>`, wedging every subsequent project merge with `INTEGRATION_DIRTY`:
  1. `pre_empty_dirs` / `pre_untracked` snapshots were taken *after* `git merge --no-ff --no-commit` and only when `test_argv` was set, so a task removing a gitlink with failing tests restored the dir on `merge --abort` and immediately `rmdir`'d it.
  2. `decide_recovery` defaulted missing `pre_empty_dirs.json` (`None`) to `set()`, wiping all tracked gitlink dirs (and unrelated pre-existing empty dirs) during `test_command="none"` or early crash recovery.
  3. `_restore_tracked_from_ref` recreated deleted `160000` dirs, and the immediately following `_cleanup_new_untracked` call `rmdir`'d them again.
- **Root-cause fixes (`mesh/core/paths.py`, `mesh/daemon/merge.py`):**
  - Added `skip_paths_real: Optional[Set[str]] = None` to `make_owned_dirs_writable` in `mesh/core/paths.py` so chmod traversal skips protected gitlink realpaths.
  - Added `_EMPTY_TREE_SHA` (`4b825dc642cb6eb9a060e54bf8d69288fbee4904`), `_gitlink_paths(integ, refs=..., include_index=...)`, `_is_under_gitlink`, `_gitlink_ancestors`, and `_gitlink_skip_paths_real` in `mesh/daemon/merge.py` (using allow-listed `git diff --raw --no-renames -z` against the empty tree / refs / `--cached`).
  - Moved persistent `pre_untracked` and `pre_empty_dirs` snapshotting *before* `git merge --no-ff --no-commit` for all non-noop merges (including `test_command="none"`), while taking separate in-memory `pre_test_untracked` / `pre_test_empty_dirs` post-merge when `test_argv` is set so newly merged empty/gitlink dirs remain protected during test cleanup and `_post_test_dirty_paths`.
  - Kept `effective_empty = _load_pre_empty_dirs(run_dir)` as `None` when absent in `decide_recovery`, skipping `_cleanup_empty_dirs` and post-cleanup empty-dir dirty checks whenever `pre_empty_dirs is None`.
  - Excluded all `160000` gitlink paths, descendants (`_is_under_gitlink`), and ancestor directories (`_gitlink_ancestors`, passed to `_safe_unlink_untracked(..., protected_dirs=...)`) across `make_owned_dirs_writable`, `_cleanup_new_untracked`, `_cleanup_empty_dirs`, `_post_test_dirty_paths`, and `_abort_and_verify`.
- **Justification for updating `tests/unit/test_batch7_pr45_r2.py:867-914`:** Under `r2`, that test called `decide_recovery(..., run_dir=None)` while asserting `crash_empty` was deleted—relying on the buggy `effective_empty = set()` fallback that caused P1-2. The test now supplies a `run_dir` containing `pre_untracked.json` and `pre_empty_dirs.json` for the cleanup verification, and adds an explicit sub-check asserting `run_dir=None` leaves `preexisting_empty` intact on disk while still restoring tracked edits cleanly.

### 3. P2-1 — Crash window after `git merge --abort` removes `MERGE_HEAD` leaving dirty tracked/untracked/`0o000` artifacts
- **Verification against code and docs:** `docs/plan.md` (§11.7 step 5) requires boot recovery of a `merging` task to leave `integ` clean. On `7f95410`, `_abort_and_verify` ran `git merge --abort` (which clears `MERGE_HEAD` and exits `0` even when tests modified/deleted uninvolved tracked files, created untracked files, or `chmod 0o000`'d dirs), then performed cleanup afterward. A `SIGKILL` right after `git merge --abort` sent `decide_recovery` down the `not _merge_head_present(integ)` branch, which checked `_dirty_paths` without calling `_restore_tracked_from_ref`, `_cleanup_new_untracked`, or `make_owned_dirs_writable`, leaving `restored=False` and wedging subsequent merges.
- **Root-cause fixes (`mesh/daemon/merge.py`):**
  - Added testing-only `CRASH_AFTER_MERGE_ABORT = "after_merge_abort"` and `_maybe_crash_after_merge_abort(run_dir)` inside `_abort_and_verify` (triggered only when `supervisor._testing_mode()` and `MESH_TEST_CRASH_AT == "after_merge_abort"` right after `cp.returncode == 0` / `cp2.returncode == 0` removes `MERGE_HEAD`). Passed `run_dir=plan.get("run_dir")` at every `_abort_and_verify` call site in `_run_merge_pipeline` and `decide_recovery`.
  - Updated the `not _merge_head_present(integ)` branch in `decide_recovery`: when the merge was neither reconciled as committed nor `head_moved` (`head == true_pre and not head_moved`) and either `effective_pre is not None` or `_dirty_paths(integ)` is non-empty:
    - If `effective_pre is not None`, runs bounded `make_owned_dirs_writable(..., skip_names={".git"}, skip_paths_real=skip_reals)`.
    - Always runs `_restore_tracked_from_ref(integ, ref=true_pre)`.
    - Only when `effective_pre is not None` runs `_cleanup_new_untracked(integ, effective_pre, pre_empty_dirs=effective_empty, protected_refs=(true_pre,), include_index=True)` (so pre-existing user untracked files and empty dirs are never deleted when snapshots are missing).
    - Re-computes `dirty` (including new empty dirs outside `effective_empty` and gitlinks when `effective_empty is not None`).

### 4. P2-2 — `workspace.prune` `commits_unknown` on deleted worktree dirs & checkpoint lag / duplicate/lost audit
- **Verification against code and docs:** `docs/usage.md` (§8) and `docs/plan.md` (§10.7, §13.4) state that `workspace delete` retains the branch while `workspace prune` cleans `merged` and zero-commit `cancelled` tasks without losing audit records or idempotent results. On `7f95410`:
  1. `_commits_beyond_base` returned `None` whenever `not os.path.isdir(workspace)`, permanently marking zero-commit `cancelled` tasks whose worktree dir had been removed (via `workspace delete` or a crash between `git worktree remove` and `git branch -d`) as `skipped(reason="commits_unknown")`.
  2. `execute_workspace_prune_plan` wrote per-item checkpoints *after* physical deletion and wrote `"completed": True` in a separate post-loop write, losing `workspace.pruned` audit events or returning `removed: []` on idempotent retry after crashes in those windows.
- **Root-cause fixes (`mesh/daemon/handlers/run.py`, `mesh/daemon/recovery.py`):**
  - Updated `_commits_beyond_base(workspace, base_commit, *, branch=None, integ_wt=None)` and callers `_prune_skip_reason` / `_prune_skip_reason_from_parts`: when the workspace dir is absent but `branch` and `integ_wt` exist, verifies `refs/heads/<branch>` via allow-listed `git show-ref --verify --quiet` and counts commits via `git log --format=%H <base_commit>..refs/heads/<branch>` in `integ_wt`. Cases where `base_commit` or the ref cannot be resolved still return `None` (`commits_unknown`).
  - Updated `plan_workspace_prune` and `run_workspace_prune` TX1:
    - Excludes tasks already satisfying `_already_pruned(task, integ_wt)` from `plan["items"]`.
    - Persists `planned` candidate metadata (`task_id`, `project_id`, `status`, `path`, `branch`, `repo_path`, `integration_branch`, `matched_root`) in the `ws_res:prune_*` reservation row.
  - Updated `execute_workspace_prune_plan`:
    - Writes `"in_flight": entry` atomically to the checkpoint *before* `_remove_worktree_and_branch_with_roots`.
    - Folds `"completed": is_last` into the final item's checkpoint write (and writes `"completed": True` when `items` is empty), eliminating the post-loop write window.
    - Added 4 testing-only `MESH_TEST_CRASH_AT` points to `_WS_CRASH_POINTS`: `before_ws_prune_delete`, `after_ws_prune_worktree_remove`, `after_ws_prune_delete`, and `after_ws_prune_last_delete`.
  - Added shared `reconcile_prune_checkpoint(conn, home, doc, ckpt)` used by both `recovery._step_ws_reservations` (`kind == "prune"`) and `run_workspace_prune` (`except BaseException:`):
    - Preserves already-checkpointed `removed` and `skipped` entries.
    - When `not ckpt_completed`, checks `ckpt["in_flight"]` plus `doc["planned"]` against `_already_pruned`, promoting any physically removed candidate to `removed` once.
    - Derives `completed = True` when `ckpt_completed` OR (`bool(planned_list)` and every planned `task_id` is covered in `removed | skipped`), ensuring an empty-plan crash before `execute_workspace_prune_plan` still rolls back instead of finalizing falsely.
  - Added `(task_id, request_id)` duplicate-event checks in both `recovery._step_ws_reservations` and `run_workspace_prune` `except BaseException:` before emitting `workspace.pruned`, guarantee-checking at most one audit event per pruned workspace.

---

## Audit Pass Across Related Merge & Workspace Code

During a full pass over `mesh/daemon/merge.py`, `mesh/core/paths.py`, `mesh/core/gitops.py`, `mesh/daemon/handlers/run.py`, and `mesh/daemon/recovery.py`, four additional bug instances in the same classes were identified and fixed with regression tests:

1. **`run_workspace_delete` Phase 2 `except BaseException:` window (`mesh/daemon/handlers/run.py`):** If `rmtree_allow_readonly` removed the workspace directory and an exception/cancellation occurred immediately afterward inside `_do_delete_sync` (for example in `git worktree prune`) before returning to TX2, the prior handler deleted `ws_res:task:*` without consuming the pinned approval or recording `workspace.deleted`. Now checks disk existence across `(real, stripped, path)`; when the directory is already gone, runs best-effort `git worktree prune`, transitions any pinned `'approved'` approval to `'consumed'`, and emits a `system` `workspace.deleted` audit event before releasing `ws_res:task:*`.
2. **`run_workspace_prune` Phase 2 `except BaseException:` checkpoint lag (`mesh/daemon/handlers/run.py`):** Previously read only `ckpt_doc.get("removed")`, sharing the same `in_flight` / checkpoint-write lag bug as boot recovery. Updated to call `reconcile_prune_checkpoint` with `(task_id, ctx.request_id)` event deduplication before releasing `ws_res:*`.
3. **`_restore_single_tracked_path` handling of `160000` gitlinks (`mesh/daemon/merge.py`):** Added an early `return` when `old_mode == "160000"` and `target` is already a non-symlink directory, preventing accidental `rmtree_allow_readonly` on an initialized submodule directory while still restoring deleted gitlink dirs (`os.makedirs`) or replacing rogue symlinks/files.
4. **`_remove_worktree_and_branch_with_roots` stale git worktree metadata (`mesh/daemon/handlers/run.py`):** When `real_path` is already absent on disk (e.g., after `workspace delete` or a crash at `after_ws_prune_worktree_remove`), executes best-effort `git worktree prune` in `integ_wt` prior to `git branch -d` so stale worktree administrative files under `.git/worktrees` cannot block branch deletion.

**Docs/Usage note:** `docs/usage.md` lines 586–588 still contain historical wording ("Mesh does not clean those files for you"); kept unchanged as in R1/R2 per `docs/plan.md` §10.5 and existing doc-check unit tests (`test_docs_m3.py`, `test_docs_m19.py`).

---

## Crash-Point Tables

### A. Merge Pipeline & Boot Recovery Crash Points (`mesh/daemon/merge.py`)

| Point / Window | State on Disk & Git | Boot Recovery Behavior (`decide_recovery` / `_step_merging`) | Task & Audit Outcome |
|---|---|---|---|
| Crash before `git merge --no-ff --no-commit` | `integ` clean, `MERGE_HEAD` absent, `pre_untracked.json` / `pre_empty_dirs.json` either absent or created right before merge | `not _merge_head_present`, `head == true_pre`, no dirty paths | Task `merging -> merge_failed` (`interrupted`, `restored=True`), `merge:<prj>` released |
| Crash during `merge --no-commit` or during `run_test_step` (`MERGE_HEAD` present) | `MERGE_HEAD` present; `pre_untracked.json` and `pre_empty_dirs.json` present | Reads `pre_untracked.json` (`set()` fallback if missing) and `pre_empty_dirs.json` (`None` preserved if missing); calls `_abort_and_verify` (bounded chmod skipping `.git` & gitlinks, index `:0:` restore on abort retry, tracked restore from `true_pre`, `_cleanup_new_untracked`, skips `_cleanup_empty_dirs` if `None`) | Task `merge_failed` (`interrupted`, `restored=True`), `160000` gitlinks preserved, `merge:<prj>` released |
| Crash right after `git merge --abort` (`MESH_TEST_CRASH_AT=after_merge_abort`) in live abort or boot-1 recovery | `MERGE_HEAD` removed, uninvolved tracked edits, untracked files, and `0o000`/empty dirs may remain on disk | `not _merge_head_present`, `head == true_pre`, `not head_moved`, `effective_pre is not None` -> bounded chmod (excluding gitlinks), `_restore_tracked_from_ref(true_pre)`, `_cleanup_new_untracked(effective_pre, effective_empty)`, empty-dir dirty check | Task `merge_failed` (`interrupted`, `restored=True`), idempotent across repeated boots, pre-existing files & gitlinks preserved |
| Crash without `MERGE_HEAD` and without `pre_untracked.json` (`run_dir=None` or pre-snapshot crash) | `MERGE_HEAD` absent; tracked edits restored if dirty, `preexisting_empty` / `user_note.tmp` preserved | `effective_empty is None` and `effective_pre is None` -> restores dirty tracked files from `true_pre`, never deletes unknown untracked files or pre-existing empty dirs / gitlinks | Clean if no pre-existing untracked files; otherwise `restored=False` listing preserved user untracked files |
| Crash when `head_moved=True` (unexpected task-related commit) | `HEAD` advanced to non-matching commit | Does not alter worktree (`dirty = pre_check_dirty`) | Task `merge_failed` (`interrupted`, `head_moved=True`, `restored=False`) |
| Crash right after `git commit --no-edit` (`MESH_TEST_CRASH_AT=after_merge_commit`) | `HEAD != pre_head`, merge commit parent[1] == task tip and message contains `task_id` | Reconciles merge commit (`outcome="merged"`, `noop=False`, `pre_head=parents[0]`) without touching gitlinks/tree | Task `merging -> merged`, idempotency / merge event finalized |

### B. Workspace Delete & Prune Crash Points (`mesh/daemon/handlers/run.py`, `mesh/daemon/recovery.py`)

| Point / Window | Reservation & Checkpoint State | Recovery / Handler Behavior | Approval, Audit & Idempotency |
|---|---|---|---|
| `after_ws_reserve` (delete) | `ws_res:task:<id>` present; workspace dir still on disk (`still_exists=True`) | `_step_ws_reservations` emits `workspace.delete_rolled_back` (`recovered=True`), deletes `ws_res:*` | Approval stays `approved`, no `workspace.deleted`, no idempotency stored; retry deletes cleanly |
| Delete Phase 2 `BaseException` before vs. after `rmtree_allow_readonly` | `ws_res:task:<id>` present; `still_exists` True (pre-rmtree) or False (post-rmtree) | Pre-rmtree: clears `ws_res:*` only. Post-rmtree: runs `git worktree prune`, marks pinned `approved` approval `consumed`, records `system` `workspace.deleted`, clears `ws_res:*` | Re-raises error; post-rmtree audit & approval consumption never lost |
| `after_ws_delete` (delete) | `ws_res:task:<id>` present; workspace dir absent (`still_exists=False`) | `_step_ws_reservations` runs `git worktree prune`, consumes approval, emits `workspace.deleted` (`recovered=True`), stores `exit_code=0` idempotency (`deleted=True`), deletes `ws_res:*` | Same idempotency key replays `deleted=True` cleanly; approval `consumed` |
| `after_ws_prune_reserve` (prune, empty `planned=[]` or non-empty) | `ws_res:prune_*` present; ckpt `{completed: False, removed: [], in_flight: None}` | Non-removed candidates remain intact on disk; `reconcile_prune_checkpoint` returns `completed=False` (even if `planned=[]`) -> emits `workspace.prune_rolled_back`, unlinks ckpt & `ws_res:*` | No false idempotency stored; retry re-runs prune cleanly |
| `before_ws_prune_delete` | Ckpt `{in_flight: entry, completed: False}`; worktree & branch still exist | `_already_pruned` is False -> stays rolled back, no `workspace.pruned`, unlinks ckpt & `ws_res:*` | Audit clean (`0` events); retry with same idempotency key succeeds |
| `after_ws_prune_worktree_remove` | Ckpt `{in_flight: entry, completed: False}`; worktree dir gone, branch still exists | `_already_pruned` is False (branch exists) -> rolls back with `0` `workspace.pruned` events; subsequent prune uses `integ_wt` (`worktree prune` + `log --format=%H` + `branch -d`) | Zero partial loss; retry removes branch cleanly |
| `after_ws_prune_delete` (non-last item crashed before ckpt update) | Ckpt `{in_flight: item_k, completed: False}`; `item_k` physically removed (dir & branch gone); remaining planned items still exist | `reconcile_prune_checkpoint` promotes `item_k` via `_already_pruned`, leaves `completed=False`, emits 1 `workspace.pruned` (`recovered=True`) for `item_k`, rolls back overall prune without storing idempotency | Next retry with same key skips `item_k` via `_already_pruned` and prunes remaining items without duplicating `item_k` event |
| `after_ws_prune_last_delete` | Ckpt `{in_flight: item_last, completed: False}`; all planned items physically removed or covered | `reconcile_prune_checkpoint` promotes `item_last` AND derives `completed=True` (`all(p in covered for p in planned_list)`), emits `workspace.pruned` once per item (deduped by `(task_id, request_id)`), stores full `removed`/`skipped` idempotency, deletes `ws_res:*` & ckpt | Retry with same idempotency key returns `idempotent_replay=True` with full `removed` list; zero duplicate events |
| `after_ws_prune` (post-loop before TX2) | Ckpt `{completed: True, removed: [...], skipped: [...]}` | `reconcile_prune_checkpoint` keeps `completed=True`, emits missing `workspace.pruned` events (deduped by `(task_id, request_id)`), stores idempotency, deletes `ws_res:*` & ckpt | Full finalized idempotent replay; zero duplicate events |
| Prune Phase 2 `BaseException` | Partial removals on disk, `in_flight` possibly removed after `_remove_worktree_and_branch_with_roots` | TX runs `reconcile_prune_checkpoint`, records `workspace.pruned` once per reconciled removed item (deduped by `(task_id, request_id)`), deletes `ws_res:*`, unlinks ckpt | Re-raises error; zero lost or duplicated `workspace.pruned` events |
| TX2 `check_authz` superseded (`workspace.delete` / `workspace.prune`) | Disk deletion already completed | Consumes pinned approval (for delete), emits `system` `workspace.deleted` / `workspace.pruned` audit events, deletes `ws_res:*`, commits before re-raising authz error | Audit matches physical disk state; reservation never wedged |

---

## Modified and Added Files

- **Production (`mesh/`):**
  - `mesh/core/paths.py`
  - `mesh/daemon/merge.py`
  - `mesh/daemon/handlers/run.py`
  - `mesh/daemon/recovery.py`
- **Tests (`tests/`):**
  - `tests/unit/test_batch7_pr45_r2.py` (lines 867–914 updated for P1-2 semantics)
  - `tests/unit/test_batch7_pr45_r3.py` (new, 15 unit tests)
  - `tests/integration/test_batch7_pr45_r3.py` (new, 4 daemon/subprocess integration tests)
- **Documentation (`docs/`):**
  - `docs/impl-notes-batch7-pr45-r3.md`

---

## Verification

All checks below ran cleanly after the final code edit (`scripts/test.sh` was intentionally not run per task instructions):

- **Baseline failing-first verification on `cloud/pr45r3-base` (`7f95410`):** With PR #45 r3 code fixes stashed, `python3 -m unittest tests.unit.test_batch7_pr45_r3` produced **14/15 failures** (only the intentional positive control `test_task_adds_new_gitlink_and_passing_tests_preserves_gitlink` passed).
- **Repeated PR #45 r3 regression runs** (`python3 -m unittest tests.unit.test_batch7_pr45_r3 tests.integration.test_batch7_pr45_r3`, 15 unit + 4 integration = **19 tests**):
  - Run 1: **19 tests in 28.696s — OK**
  - Run 2: **19 tests in 29.085s — OK**
  - Run 3: **19 tests in 29.067s — OK**
- **Related 28-module unit + recovery batch** (`python3 -m unittest tests.unit.test_batch7_pr45_r3 tests.unit.test_batch7_pr45_r2 tests.unit.test_batch7_pr45_r1 tests.unit.test_batch7_g4 tests.unit.test_batch7_pr43_r1 tests.unit.test_batch7_pr43_r2 tests.unit.test_batch7_pr43_r3 tests.unit.test_batch7_pr44_r1 tests.unit.test_batch7_pr44_r2 tests.unit.test_merge_recovery_minors tests.unit.test_paths tests.unit.test_gitops tests.unit.test_invariants tests.unit.test_recovery_units tests.unit.test_repos tests.unit.test_repos_cas tests.unit.test_full_review_fixes tests.unit.test_dogfood_core_bughunt tests.unit.test_dogfood_merge_held tests.unit.test_m4_p3_cleanup tests.unit.test_batch6_shutdown tests.unit.test_settings tests.unit.test_docs_m3 tests.unit.test_docs_m19 tests.recovery.test_recovery tests.recovery.test_recovery_adopt_seen tests.recovery.test_recovery_lead_merge tests.recovery.test_recovery_merge`):
  - **279 tests in 140.608s — OK**
- **Related 13-module integration batch** (`python3 -m unittest tests.integration.test_batch7_pr45_r3 tests.integration.test_batch7_pr45_r1 tests.integration.test_batch7_pr45_r2 tests.integration.test_batch7_g4 tests.integration.test_deliver_prune tests.integration.test_merge tests.integration.test_merge_tests tests.integration.test_code_tasks tests.integration.test_refs_worktrees tests.integration.test_approvals tests.integration.test_delegated_approval tests.integration.test_daemon tests.integration.test_merge_refs`):
  - **120 tests in 285.132s — OK**
- **Note on pre-existing `tests.unit.test_merge` wall-clock TTL failure (left untouched outside PR #45 r3 scope):**
  - `python3 -m unittest tests.unit.test_merge` ran **21 tests in 2.543s** with **20 passed** and **1 pre-existing failure**: `ReviewR5bR6MinorsTest.test_duplicate_idempotency_key_keeps_the_mutation` (`tests/unit/test_merge.py:225,237`).
  - Root cause verified against `7f95410`: neither `tests/unit/test_merge.py` nor `mesh/daemon/server.py` was modified in PR #45 r3; the test seeds `created_at = NOW` (`"2026-11-09T00:00:00Z"`), while `mesh/daemon/server.py:_store_idempotency_row` deletes rows older than `IDEMPOTENCY_TTL_S = 7 * 86400` relative to `time.time()`. Once the VM UTC clock crossed `2026-11-16T00:00:00Z`, that 7-day-old fixture row expired and was pruned prior to `INSERT`, making `stored` `True` instead of `False`.
- **Hygiene & bytecode compile checks:** `git diff --check` and `python3 -m py_compile` across all modified/added Python files (`mesh/core/paths.py`, `mesh/daemon/merge.py`, `mesh/daemon/handlers/run.py`, `mesh/daemon/recovery.py`, `tests/unit/test_batch7_pr45_r2.py`, `tests/unit/test_batch7_pr45_r3.py`, `tests/integration/test_batch7_pr45_r3.py`) exited `0`.
