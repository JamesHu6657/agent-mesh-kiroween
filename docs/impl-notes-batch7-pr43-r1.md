# Batch 7 PR #43 Review R1 Implementation Notes

This note records verification results and design boundaries for the six findings in `/home/user/blind/b7/pr43-review.md` on branch `pr43-r1`.

## Verified and Fixed Findings

1. **P1-1 (`task reject` permission escalation, compound gate, `op_hash` binding, and spec prompt length):** Confirmed real.
   - `mesh/core/approvals.py`: Added `"task.reject"` to `get_row_approval_policy` compound-gate handling.
   - `mesh/core/prompts.py`: Added shared `deliverable_summary()` helper reused by `read_rework_feedback()` and `handle_task_reject()`.
   - `mesh/daemon/handlers/task.py`: `handle_task_reject` now computes `need_perm` for direct user-only `permission_escalation`, enforces `_check_spec_prompt_length` and `_validate_model_and_effort` for `launch` assignees, and binds `assignee` and `profile` into `params`.
2. **P1-2 (`task retry` `op_hash` missing `assignee` and `profile`):** Confirmed real.
   - `mesh/daemon/handlers/task.py`: Bound `assignee` (`assignee_name`) and `profile` (`resolved_profile`) into `params` passed to `_require_task_approval` in `handle_task_retry`.
3. **P1-3 (`supervisor.seen_held` permanent exclusion on recovered `spawn_abort_pending`):** Confirmed real.
   - `mesh/daemon/supervisor.py`: Removed the persisted `_SEEN_REASON.get(run_id) == "spawn_abort_pending"` short-circuit in `seen_held()` while retaining the in-process `run_id in _SPAWN_ABORTS` guard so recovered runs held only due to a `spawn_abort_pending` marker can be released with `mesh run release --force-seen-unusable`.
4. **P2-1 (`_check_lead_run_token` rejecting completed/paused projects):** Confirmed real.
   - `mesh/core/authz.py`: Changed `_check_lead_run_token` for `LEAD_RUN_TOKEN_MUTATING_CMDS` from `pstatus != "active"` to `pstatus is None or pstatus == "cancelled"`, aligning live lead run tokens with persistent lead tokens so `project.complete` followed by `project.deliver` / `workspace.prune` in the same lead turn and paused-project operations succeed while cancelled projects stay blocked.
5. **P2-2 (`lead_turn` already-exited / leftovers / `starting` cancellation window):** Confirmed real.
   - `mesh/daemon/server.py`: `_terminate_lead_turn` now upgrades an active `Termination` whose `end_reason == "leftovers"` to `"project_cancelled"` / `"lead_superseded"` and checks `run["wrapper_pid"] or pgid` using `supervisor.close_identity(run)`.
   - `mesh/daemon/supervisor.py`: `_begin_termination` falls back to `close_identity(run)` when DB `pgid` / `wrapper_pid` / `proc_starttime` are still `None`; `_finalize_termination` rechecks `lead_turn_termination_reason(conn, run)` before executing `finish_lead_turn` when `t.end_reason == "leftovers"` (preserving `RECOVERY_END_STATUS` such as `crash_recovery` -> `lost`); `_watch_lead_turn` (`liveness == "gone"`) rechecks `lead_turn_termination_reason(conn, run)` for `("project_cancelled", "lead_superseded")` before starting `"leftovers"` termination or calling `finish_lead_turn`, routing cancelled/superseded turns to `_finalize_lead_turn_termination`.

## P2-3 Partial Fix and Non-Applied Sub-Claim

**Finding summary:** P2-3 claimed `task retry` (and launch `task reject`) without `--model` or `--effort` skips rechecking existing values against `allowed_models` (citing `strict_models`) and `allowed_efforts`.

- **Real bug (fixed):** **`allowed_efforts` recheck when `--effort` is omitted.**
  - Neither `scheduler.check_queued_tasks_t6` (`mesh/daemon/scheduler.py`) nor `supervisor` / `handle_task_start` (`mesh/daemon/handlers/task.py:2351`) checks `allowed_efforts`. Without rechecking at `task retry` / launch `task reject`, tightening an agent's `allowed_efforts` via `agent update` allowed `task retry` or launch `task reject` to spawn a child process with the disallowed `task["effort"]`.
  - Fixed in `mesh/daemon/handlers/task.py` (`_validate_model_and_effort`) by rechecking `resolved_effort not in allowed_efforts` whenever `resolved_effort` is set and `allowed_efforts` is non-empty (after the adapter `reasoning_effort` capability check), and calling `_validate_model_and_effort` on `launch` assignees in `handle_task_reject`.

- **Not applied (not a bug / contradicts existing tests & schema):** **Rejecting existing `task["model"]` when `--model` is omitted (`strict_models`).**
  1. **`strict_models` does not exist:** `agents` in `mesh/core/schema.py:29` has no `strict_models` column, and `handle_agent_update` explicitly rejects `--strict-models` as an unimplemented fixed/unsupported field (`mesh/daemon/handlers/agent.py:487-488`, documented as deferred in `docs/m3-gate-report.md:147` and `docs/impl-notes-m3-gatefix.md:344`). In `docs/plan.md:652`, `--strict-models` referred to failing `agent create/update` when `adapter.list_models()` reports unknown model IDs, not blocking `task retry` without `--model`.
  2. **Models are already guarded at T6 and T5:** Unlike `effort`, `task["model"]` is re-checked against `allowed_models` in `scheduler.check_queued_tasks_t6` (`mesh/daemon/scheduler.py:542`) and `handle_task_start` (`mesh/daemon/handlers/task.py:2351`), so a task cannot start with a disallowed model.
  3. **Existing integration tests explicitly depend on `task retry` without `--model` reaching T6:**
     - `tests/integration/test_tasks.py:1396-1445` (`test_repeated_task_transitions_outbox_dedup_and_inbox_delivery`) disables `m-x` via `agent update`, waits for T6 failure, and runs `task retry` without `--model` twice (asserting `rc == 0` so T6 fails again and emits duplicate notifications).
     - `tests/integration/test_tasks.py:1457-1501` (`test_task_retry_effort_unsupported_on_vibe_adapter`) disables `m-orig` via `agent update`, waits for T6 failure, and runs `task retry --effort low` without `--model`, asserting `EFFORT_UNSUPPORTED` rather than `MODEL_NOT_ALLOWED`.
