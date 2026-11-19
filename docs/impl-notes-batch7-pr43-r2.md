# Batch 7 PR #43 Round-2 Review Notes

All four findings reported in `/home/user/blind/b7/pr43r2-review.md` were verified as real bugs against code and documentation and fixed cleanly with regression tests in `tests/unit/test_batch7_pr43_r2.py` and `tests/integration/test_batch7_pr43_r2.py`.

## Findings Verified and Fixed

1. **P1 — Disabled lead agent's already-queued `task.merge` ran tests and merged:**
   - **Root cause:** `DaemonServer._run_lead_merge` bypasses `authenticate_and_authorize`, while `authz.check_authz` and `_check_lead_run_token` did not check `principals.disabled_at`. In addition, `lead_turns.recover_queued_lead_merges` discarded the error returned by `_lead_turn_principal`, while `_lead_turn_principal` checked `revoked_at` (set normally on lead turn close) before `disabled_at`.
   - **Fix:**
     - `mesh/core/authz.py`: added `principals.disabled_at` enforcement in `check_authz` (for SQLite connections and non-user principals) and in `_check_lead_run_token` (`current["disabled_at"]`), raising `TOKEN_INVALID` (`"Principal is disabled"`).
     - `mesh/daemon/lead_turns.py`: in `recover_queued_lead_merges`, reject disabled principals (checking `principals.disabled_at` directly so `revoked_at` from normal turn close neither bypasses disabled checks nor blocks enabled recovery) and record a terminal `lead.action_result` (`ok=False, error_code=TOKEN_INVALID`).

2. **P2 — `allowed_efforts` recheck missed empty `[]` allowlist and inbox-mode `task reject`/`task start` (plus launch T6):**
   - **Root cause:** `_validate_model_and_effort`, `handle_agent_create`, and `handle_agent_update` gated default/current effort membership behind `and allowed_efforts`, treating `[]` as unrestricted. `mesh/cli/commands/agent.py` sent `default_effort: None` even when omitted while `handle_agent_update` ignored `None`, making it impossible to clear `default_effort` via `--input` JSON before clearing `allowed_efforts` to `[]`. `handle_task_reject` gated `_validate_model_and_effort` on `delivery == "launch"`, and `handle_task_start` and `scheduler.check_queued_tasks_t6` checked models only.
   - **Fix:**
     - `mesh/daemon/handlers/task.py`: removed `and allowed_efforts` in `_validate_model_and_effort`; moved `_validate_model_and_effort` out of the `delivery == "launch"` branch in `handle_task_reject`; added effort allowlist (and empty `allowed_models` when `task_model` is set) validation to `handle_task_start`.
     - `mesh/daemon/handlers/agent.py` & `mesh/cli/commands/agent.py`: enforced `default_effort not in allowed_efforts` even when `allowed_efforts == []`; omitted unset `"default_effort"` in CLI `agent.update` payload unless given or in `_from_json` so explicit JSON `"default_effort": null` (and `""`) clears `default_effort` cleanly.
     - `mesh/daemon/scheduler.py`: in `check_queued_tasks_t6`, added `effort_not_allowed` check for queued `launch` tasks (`resolved_effort not in allowed_efforts`) so launch tasks blocked behind a busy slot never spawn with a removed effort (keeping `inbox` effort checks on `task start` / `task reject` so CLI callers reliably see `EFFORT_NOT_ALLOWED`).

3. **P2 — Draining queued FIFO `task.merge` after `project cancel` inserted a pending `lead_wake`:**
   - **Root cause:** `handle_project_cancel` did not clear `DaemonServer._project_merge_queues`, and `_get_project_lead_info` in `mesh/core/events.py` did not exclude `'cancelled'` projects, so `record_queued_merge_failure` fanned out `lead.action_failed` to a fresh `status='pending'` `outbox` row.
   - **Fix:**
     - `mesh/daemon/handlers/project.py` & `mesh/daemon/server.py`: emitted and handled `Effect("cancel_project_merges", ...)` to clear `_project_merge_queues[project_id]` and `_project_merge_inflight_reqs` on cancel, and guarded `_drain_project_lead_merges` against `'cancelled'` projects.
     - `mesh/daemon/lead_turns.py`: in `recover_queued_lead_merges`, record `PROJECT_NOT_ACTIVE` failure without re-queueing when `projects.status == 'cancelled'`.
     - `mesh/core/events.py`: updated `_get_project_lead_info` to require `pr.status != 'cancelled'`, preventing `fan_out_event` from creating pending outbox rows for cancelled projects.

4. **P3 — Unit test wrote executable stub in `/tmp` (fails on `noexec` `/tmp`):**
   - **Root cause:** `TestBatch7PR43R1Unit.test_p1_1_reject_checks_spec_prompt_length_for_launch` in `tests/unit/test_batch7_pr43_r1.py` created and `chmod 0o755` a `kiro-cli` script inside `self.home` (`/tmp`), causing `os.access(..., os.X_OK)` to fail with `ADAPTER_NOT_INSTALLED` on `noexec` `/tmp`.
   - **Fix:** Updated `tests/unit/test_batch7_pr43_r1.py` to point `binary_path` at the existing repository tripwire shim `tests/shims/kiro-cli`.
