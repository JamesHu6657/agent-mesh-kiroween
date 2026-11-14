# Implementation Notes — Milestone Gate M1c

Status: In Progress (Gate M1c — Execution)
Implementer: agy (Gemini 3.8 Flash High)
Date: 2026-11-08

---

## 1. Run 1 (Parallel Runner, M1b Carry-overs, Gitops Allow-list, Inbox-Mode Execution, Lead Reviews)

### 1.1 Parallel Test Runner & A1 Budget (Sub-step 1)
- **Runner**: New test runner script `tests/run_tier.py` for integration and recovery tiers.
  - Test enumeration using `unittest.TestLoader().discover(<dir>, top_level_dir=".")`.
  - Process isolation: runs each test in its own subprocess (`python3 -m unittest <id>`) with `start_new_session=True` and a per-test timeout (`MESH_TEST_TIMEOUT_S`, default 600s). On timeout, kills the process group with `SIGKILL`.
  - Concurrency: worker count determined by `MESH_TEST_JOBS` if set, otherwise `max(1, os.cpu_count())` (4 workers on this 4-CPU host).
  - Serial test marker: `tests.helpers.serial` marks timing-sensitive tests that must run sequentially.
  - Output: prints status line per test (`ok|FAIL|ERROR <id> <secs>s`), full output of any failures, top 10 slowest tests, and standard `Ran N tests in X.XXXs` format.
- **Timing Comparisons**:
  - Baseline (serial runner):
    - Unit tier: 99 tests in 7.632s
    - Integration tier: 121 tests in 410.700s
    - Recovery tier: 1 test in 0.000s
    - Total test suite wall time: ~420s (7m00s)
  - Parallel runner (3 workers, the first runner configuration):
    - Unit tier: 99 tests in 7.990s
    - Integration tier: 121 tests in 230.440s
    - Recovery tier: 1 test in 0.044s
    - Total test suite wall time: 238s (< 4 minutes)
  - Later runs use `os.cpu_count()` workers (4 here); the measured wall times are in "Final Test Suite Metrics" below.
- **Serial Marked Tests** (14 tests marked with `@serial`):
  - `tests/integration/test_daemon.py`: `test_passphrase_lockout` (sets `auth.lockout_window_s=10` and polls for the lockout to lapse)
  - `tests/integration/test_projects.py`: `test_project_wait_terminal_and_timeout` (duration check)
  - `tests/integration/test_tasks.py`: `test_task_wait_state_reached_and_timeout` (duration check)
  - `tests/integration/test_events.py`: `test_event_tail_disconnect_releases_stream` (duration check)
  - `tests/integration/test_inbox.py`: `test_j2_inbox_wait_timeout_empty` (duration check)
  - `tests/integration/test_inbox.py`: `test_j2_inbox_wait_returns_promptly_on_message` (duration check)
  - `tests/integration/test_inbox.py`: `test_inbox_wait_does_not_block_concurrent_requests` (duration check)
  - `tests/integration/test_inbox.py`: `test_inbox_wait_disconnect_leaves_outbox_pending` (timing sleep)
  - `tests/integration/test_approvals.py`: `test_g15_testing_mode_expiry` (TTL expiry sleep)
  - `tests/integration/test_execution_inbox.py`: `test_h6_inbox_lease_expiry_and_heartbeat` (ACC: H6; `lease.ttl_s=2` and a 5 s heartbeat window)
  - `tests/integration/test_execution_inbox.py`: `test_h7_inbox_no_response` (ACC: H7; `inbox.ack_timeout_s=2` and `task wait` timeouts)
  - `tests/integration/test_execution_inbox.py`: `test_h4_fence_mismatch_and_recovery` (ACC: H4; `lease.ttl_s=2` expiry wait)
  - `tests/integration/test_execution_inbox.py`: `test_h4_stale_task_fail_reports_lease_expired_then_fence_mismatch` (ACC: H4; `lease.ttl_s=2` expiry wait)
  - `tests/integration/test_execution_inbox.py`: `test_h15_workspace_fresh_vs_reused_after_lease_expired` (ACC: H15; `lease.ttl_s=2` expiry wait)
- **Tripwire Hygiene**:
  - `scripts/test.sh` removes `/tmp/mesh-tripwire-hit.log` and `$TRIPWIRE_FILE` before test tiers run.
  - At suite completion, asserts both tripwire files are absent or empty.

### 1.2 Coordinator Decisions (Q1–Q7)
- **Q1 (Parallel test runner)**: Approved. Parallel runner implemented in `tests/run_tier.py`; workers = `MESH_TEST_JOBS` or `os.cpu_count()` (4 here). Timing-sensitive tests marked `@serial`.
- **Q2 (Scheduler tick)**: Approved. Keep the per-tick check.
- **Q3 (Workspace verbs milestones)**: Approved. `workspace.list` and `workspace.show` report `"M1c"` (built in run 2). `workspace.prune` and `workspace.delete` report `"M2"`. `top` reports `milestone: null`.
- **Q4 (Inbox run tokens)**: Approved. External runs get no run token.
- **Q5 (Task start on launch agent)**: Approved. Starting a launch agent via `task start` returns exit code 6 `INVALID_TRANSITION` with `details.reason: "launch_mode"`.
- **Q6 (Lead workspace directories)**: Approved. Lead dirs created at P1 and set-lead.
- **Q7 (Commits count)**: Approved. All commits count towards milestones.

### 1.3 Carry-over Status
- **Review 7b N1**: Not addressed: plan-level, outside M1c scope.
- **Review 7b N2**: Gated `create_or_get_pending_approval` calls pass `request_id=ctx.request_id` (and `k_user=ctx.k_user` where caller is user) across all 5 project handlers (`set_lead`, `pause`, `resume`, `cancel`, `update`) and 3 task handlers (`create/assign`, `reassign`, `retry`). Tested in `test_approvals.py` and `test_tasks.py`.
- **Review 7b N3**: `workspace.prune` and `workspace.delete` report `"M2"`. `workspace.list` and `workspace.show` report `"M1c"`. `top` reports `milestone: null`. Asserted in `test_skeleton.py` and `test_daemon.py`.
- **Review 7b N4**: `can_principal_read_project` confirmed in use in `mesh/daemon/handlers/approval.py:731` and `:768`.
- **Review 7c Minor 1**: Added DB-insert tests in `test_approvals.py` for relayed approved row with NULL `decided_at` and decision row with NULL `decided_by`. Updated `audit.py` to ensure `relayed_summary` is included in error details upon mismatch.
- **Review 7c Minor 2**: Added `ACC: G20` tag to docstring of `test_audit_verify_and_consume_reject_null_decided_at_or_mac`.
- **Review 7c Minor 3**: Replaced `server.py:456–458` line reference in `docs/impl-notes-m1b.md` §7.6 and `docs/m1b-gate-report.md` §5.3 with "`lockout_window` in `DaemonServer.authenticate_and_authorize` (mesh/daemon/server.py)".
- **Events and Approvals Relayed Identity**: Events store `relayed_by` as the agent name (per E12 requirement), while the `approvals` table stores the relayer's principal ID.
- **User Statement Redaction**: Slated for run 2.

### 1.4 Gitops Allow-list (ACC: A6)
- **Module**: `mesh/core/gitops.py` is the single module authorized to invoke `git` subprocesses across the codebase (plan §7.5, §10.5, §11.3).
- **Validation**: `run_git` validates the arguments vector before spawning any subprocess.
  - The allow-list is the explicit constant `ALLOWED_SUBCOMMANDS` in `mesh/core/gitops.py`: `rev-parse`, `status`, `diff`, `log`, `merge-base`, `show-ref`, `cat-file`, `branch`, `worktree`, `merge`, `commit`, `update-ref`. Any subcommand not in that set (including a global `-c`) is rejected.
  - `merge` accepts exactly three forms: `["merge", "--abort"]`, `["merge", "--ff-only", <ref>]` and `["merge", "--no-ff", "--no-commit", <ref>]`. Anything else is rejected.
  - `_check_ref` rejects a ref that is empty, starts with `-`, or contains whitespace.
  - `diff` and `log` reject `--output`, `--output=<file>`, `-o` and `-o<file>` (`FORBIDDEN_OUTPUT_FLAGS` / `_is_output_flag`).
  - Disallowed commands: `push`, `rebase`, `reset`, `checkout`, `switch`, `fetch`, `pull`, `clone`, `config`, `stash`, `clean`, `rm`, `mv`, and any flags such as `-X`, `--strategy-option`, `--force`, `-f`.
  - §10.7 Prune Note: Plan §10.7 specifies prune uses `worktree remove` + `branch -d`. Until M2, `branch -d` is strictly rejected by the validator as the conservative choice.
  - Child environment sanitization: uses `build_external_env()` from `mesh.constants`.
- **Tests**: `tests/unit/test_gitops.py` (ACC: A6) tests full validation, guarantees rejection happens before subprocess spawn, ensures `git` is never spawned outside `gitops.py`, and executes a live `git rev-parse HEAD`.

### 1.5 Inbox-Mode Execution, Leases, and Fencing (Sub-step 3, ACC: D14, J3)
- **Leases & Fencing**:
  - `mesh/core/leases.py` implements `verify_task_lease_and_fence` enforcing §11.5 verification order:
    1. If no active lease for `task:<id>`: raise rc 9 `LEASE_EXPIRED`.
    2. If lease exists but presented fence != `lease.fence`: raise rc 9 `FENCE_MISMATCH`.
    3. If lease exists and presented run != `lease.run_id`: raise rc 9 `FENCE_MISMATCH`.
  - Unit tests in `tests/unit/test_leases.py` cover all negative and positive fence and expiry clauses.
- **Inbox-Mode Task Commands**:
  - `task.start`: Guard order: task exists -> actor is assignee (else rc 4 `NOT_ASSIGNEE`) -> delivery is inbox (else rc 6 `INVALID_TRANSITION`, reason: `launch_mode`, Q5) -> project active -> task status is queued -> agent concurrency limit (else rc 11 `CONCURRENCY_LIMIT`) -> global concurrency cap -> model allowed -> lease free.
    - **Deviation from `prompts/agy-m1c-run1.md`**: the prompt listed project-active before the delivery check and model-allowed before agent concurrency. The implemented order checks delivery before project-active and agent concurrency before model-allowed, so a launch-mode agent gets the `launch_mode` reason (Q5) and a saturated agent gets `CONCURRENCY_LIMIT` regardless of its model.
    - Transaction allocates run (`kind='external'`) and lease (`holder_kind='agent'`).
    - Post-commit effect creates run and workspace directories with `0700` permissions.
    - No run token issued for external runs (Q4).
  - `task.heartbeat`: Extends `leases.expires_at = now + lease.ttl_s`. Emits NO events to prevent event log pollution.
  - `task.progress`: Extends lease expiry and records `task.progress` event (message bounded to 4 KB).
  - `task.submit`: Verifies fence; validates deliverable JSON object (<= 200 KB) or summary. Deletes lease, sets run to `status='exited'`, `end_reason='submitted'`, updates task to `status='submitted'`, and emits `task.submitted` which fans out to lead.
  - `task.fail`: Verifies fence; strips `--reason` before validation; records `failure_json = {"reason": "reported", "details": {"message": reason, "run_id": ...}}`. Deletes lease, sets run to `status='exited'`, `end_reason='failed'`, updates task to `status='failed'`, and emits `task.failed` (carrying `run_id`) notifying lead.
- **Queue Info**:
  - Updated `compute_queue_info` in `mesh/daemon/scheduler.py` so queued inbox tasks show `concurrency_limit` when the agent is at capacity or global cap is reached, and `waiting_for_start` when a slot is free (§9.3).
- **Tests**:
  - `tests/integration/test_execution_inbox.py` covers ACC: D14 (concurrency limit & queue info transitions), ACC: J3 (idempotent submit replay), ACC: A8 (`doctor --check-invariants` clean while task is running), authority checks (lead and plain cannot start/submit), happy path, fail path, and stale lease/fence rejection.

### 1.6 Inbox Lease Expiry and No-Response (Sub-step 4, ACC: H4, H6, H7)
- **Lease Expiry Sweep**:
  - `sweep_expired_inbox_leases` in `mesh/daemon/scheduler.py` runs every scheduler tick.
  - Queries `leases` joined with `runs` and `tasks` where `holder_kind='agent'` and `expires_at < now`.
  - For each expired lease, in an independent transaction:
    1. Deletes the lease.
    2. Updates run to `status='lost'`, `end_reason='lease_expired'`, with `ended_at` and `result_json`.
    3. Updates task from `running` to `failed` with `failure_json={"reason": "lease_expired", "details": {"run_id": ..., "fence": ...}}`.
    4. Records event `task.failed` with reason `lease_expired`, which fans out to notify the project lead with dedup key.
  - Meshd-held leases (`holder_kind='meshd'`) are excluded and never swept here (§6.4).
- **Inbox Ack Timeout Check (`no_response`)**:
  - `check_queued_tasks_t6` checks `queued` tasks of inbox assignees where `offered_at + inbox.ack_timeout_s < now`.
  - Marks task `failed` with `failure_json={"reason": "no_response", "details": {"offered_at": ..., "timeout_s": ...}}`.
  - Supersedes open `task.offered` in outbox.
  - Records event `task.failed` with reason `no_response`, notifying project lead.
  - **Reading**: `check_queued_tasks_t6` fails a queued inbox task with `no_response` after `offered_at + inbox.ack_timeout_s`, whatever the project status or slot count. That includes tasks of paused projects and tasks waiting behind the agent's own running task (rc 11). This follows the literal T6 text; Quest decides in run 3 (E17) whether to exclude paused projects or reset `offered_at`.
- **Fence Verification Order**:
  - All four fenced writes (`task.heartbeat`, `task.progress`, `task.submit`, `task.fail`) verify the lease and fence after authz. `task.submit` and `task.fail` then check `status == "running"`, so a late write on a failed or expired task returns rc 9 `LEASE_EXPIRED` or `FENCE_MISMATCH` per §11.5 instead of rc 6 `INVALID_TRANSITION`. `task.heartbeat` and `task.progress` have no status check at all: they only extend the lease, so a stale write on them is rejected by the fence check alone.
  - `task.submit` had this order from the start. `task.fail` was fixed in `77ce885` (in run 1 it returned rc 6 `INVALID_TRANSITION` for stale writes).
- **Tests**:
  - `tests/integration/test_execution_inbox.py`:
    - `test_h6_inbox_lease_expiry_and_heartbeat` (ACC: H6): with `lease.ttl_s=2`, task without heartbeat fails with `lease_expired` and subsequent submit returns rc 9 `LEASE_EXPIRED`. Task with heartbeat stays `running` through 5s and submits cleanly with rc 0.
    - `test_h7_inbox_no_response` (ACC: H7): with `inbox.ack_timeout_s=2`, unstarted queued task transitions to `failed` with reason `no_response`, and lead receives exactly one `task.failed` notification.
    - `test_h4_fence_mismatch_and_recovery` (ACC: H4): after lease expiry and lead retry, start yields fence 2; late submit with fence 1 returns rc 9 `FENCE_MISMATCH` while fence-2 lease and running task remain unaffected; subsequent valid submit with fence 2 succeeds.

### 1.7 Lead Review, Rework, Fresh Workspace, and Task History (Sub-step 5, ACC: E11, E15, E16, F5, F11, G4, G16, H15)
- **Task Accept (`task.accept`)**:
  - Implemented in `handle_task_accept` (plan §6.2 T11):
    1. Guard: only lead or user (authz checked via `check_authz`). Lead cannot accept own task (rc 4 `LEAD_CANNOT_BE_ASSIGNEE`).
    2. Transition check: task must be in `submitted` state (else rc 6 `INVALID_TRANSITION`).
    3. Lease check: no active lease held for `task:<id>` (else rc 6 `RUN_STILL_ACTIVE`).
    4. Transition: updates task to `status='accepted'`, records `task.accepted` event with optional note, and triggers `promote_tasks` to promote newly unblocked dependents.
- **Task Reject (`task.reject`)**:
  - Implemented in `handle_task_reject` (plan §6.2 T12):
    1. Reason validation: non-empty string <= 4096 bytes (else rc 2 `INVALID_ARGUMENT`, `details.param: "reason"`).
    2. Transition check: only `submitted` or `merge_failed` -> `rework` (else rc 6 `INVALID_TRANSITION`).
    3. Lease check: no active lease held (else rc 6 `RUN_STILL_ACTIVE`).
    4. Attempt cap check: if `attempt >= max_attempts`, gated with category `retry_over_limit` (rc 7 `APPROVAL_REQUIRED` / consumption).
    5. Transition: updates task to `status='rework'`, records `task.rejected` event with reason, and triggers `promote_tasks` (T13 promotion promotes rework task back to `queued` with new `task.offered` event).
    6. Rework feedback: stored across the latest `task.rejected` event and the previous deliverable summary, without schema changes. `tasks.deliverable_json` is the wrapper `{"deliverable": {...}, "submitted_by": ..., "submitted_at": ...}`, so the rework summary is `deliverable_json.deliverable.summary`; `_format_task` unwraps `deliverable` for `task show`. Run 2's prompt renderer must read that path.
- **Fresh Workspace After Inbox Lease Expiry (§6.2, H15)**:
  - On T19 `task retry` and T20 `task reassign`, when the task's previous run ended with `end_reason='lease_expired'`:
    - If `--reuse-workspace` is provided: keeps current `workspace_path` and records `workspace: "reused"` in event payload.
    - Otherwise (default): computes new path `<workspaces.root>/<prj>/tasks/<tsk>.a<next_attempt>/`, keeps existing old directory intact, updates `tasks.workspace_path`, and records `workspace: "fresh"` in event payload.
- **Task Show History**:
  - `_format_task` populates `history`: an ordered list of the task's transition and escalation events (`task.failed`, `task.rejected`, `task.retried`, `task.reassigned`, `task.escalated`) in sequence order with `ts`, `type`, `reason`, and `attempt`.
- **Project Complete Guard Order (§6.1 P4, E15, E16)**:
  - In `handle_project_complete`, guard (d) criteria completion is checked first.
  - Guards (a), (b), (c) check tasks (must be merged, accepted, or cancelled), leases, and approvals. `COMPLETION_BLOCKED` error details report the exact blocking IDs (`details.blocking`).
  - `project.completed` payload includes `"for_user": True`, delivered to user inbox and origin agent inbox.
- **Tests**:
  - `tests/integration/test_execution_inbox.py`:
    - `test_e11_lead_transfer_authority_on_task_create_and_accept` (ACC: E11): after `set-lead lead2`, `lead1` calls to `task create` and `task accept` return rc 4 `NOT_PROJECT_LEAD`; `lead2` calls succeed with rc 0.
    - `test_e15_completion_blocked_on_submitted_task_and_pending_approval` (ACC: E15): `project complete` while task is `submitted` returns rc 6 `COMPLETION_BLOCKED` listing the task id; while approval is `pending`, returns rc 6 listing the approval id; project stays `active`.
    - `test_e16_wait_until_completed_and_completion_notifications` (ACC: E16): `project wait --until completed` in background thread; lead1 completes project; wait unblocks with rc 0; user and origin agent inboxes each receive one `project.completed` with `for_user: true`; complete call by plain agent returns rc 4.
    - `test_f5_dependency_promotes_on_accept_and_cycle_rejected` (ACC: F5): T2 depends on T1; while T1 is running/submitted, T2 remains `assigned`; once T1 is accepted, T2 promotes to `queued`; adding circular dependency returns rc 2 `DEP_CYCLE`.
    - `test_f11_rejected_task_history_and_attempt_count` (ACC: F11): task rejected and resubmitted shows attempt 2, `max_attempts: 3`, deliverable, and history entry containing the rejection reason.
    - `test_g4_lead_actions_leave_no_approval_rows` (ACC: G4): lead executes create, assign, accept, reject, and project complete; database `approvals` table has 0 rows.
    - `test_g16_executor_notes_cannot_bypass_approval_gates` (ACC: G16): deliverable notes claiming user approval cannot satisfy approval gate; op still returns rc 7 `APPROVAL_REQUIRED`; approval row remains `pending` and is never approved or consumed.
    - `test_h15_workspace_fresh_vs_reused_after_lease_expired` (ACC: H15): after lease expiry, retry without `--reuse-workspace` allocates fresh workspace ending in `tasks/<tsk>.a2/` preserving old directory; retry with `--reuse-workspace` leaves workspace path unchanged. The test also asserts the `task.retried` payloads: one event per task, `workspace: "fresh"` with the new path for the first, `workspace: "reused"` with the unchanged path for the second.

### 1.7a CLI Notes (77ce885)
- `task submit --deliverable` accepts a file path, `-` for stdin, or an inline JSON object when the argument is not an existing path. The value must be a JSON object and at most 200 KB.
- `--summary` is merged into the deliverable as its `summary` key (`setdefault`, so an explicit `summary` key in the JSON wins). The server's 200 KB check still applies after the merge.
- `task fail --reason` is stripped before validation.
- The lease-expiry `task.failed` event carries `run_id`.
- Covered by `tests/integration/test_submit_cli.py` (ACC: G16, supporting): `--deliverable <file> --summary S` yields `data.deliverable.summary == S`, and an explicit JSON `summary` wins over the flag.

### 1.8 Reconciliation and Readings Summary

#### Carry-over Status Table
| Item | Description | Status | Relevant Commit(s) |
|---|---|---|---|
| Review 7b N1 | Replay of reset consumed approval row retains valid MAC | Not addressed: plan-level, outside M1c scope | – |
| Review 7b N2 | Pass `request_id=ctx.request_id` and `k_user=ctx.k_user` to gated approvals | Resolved | `460f435` |
| Review 7b N3 | Align planned verbs milestones (`workspace.prune/delete` -> `"M2"`, `workspace.list/show` -> `"M1c"`, `top` -> `null`) | Resolved | `460f435` |
| Review 7b N4 | `can_principal_read_project` confirmed in use in `approval.py:731` & `:768` | Confirmed | `460f435` |
| Review 7c Minor 1 | Rejection of relayed row with NULL `decided_at` or decision row with NULL `decided_by` in `test_approvals.py` | Resolved | `460f435` |
| Review 7c Minor 2 | Tag `ACC: G20` on `test_audit_verify_and_consume_reject_null_decided_at_or_mac` docstring | Resolved | `460f435` |
| Review 7c Minor 3 | Line reference replacement for `lockout_window` in `impl-notes-m1b.md` and `m1b-gate-report.md` | Resolved | `460f435` |

#### Readings and Design Decisions
1. **Q1 (Parallel test runner)**: `tests/run_tier.py` runs tests concurrently with workers = `MESH_TEST_JOBS` or `os.cpu_count()` (4 here), with process-group isolation and timeouts. Wall-clock timing tests marked with `@serial`.
2. **Q2 (Scheduler tick)**: Keep per-tick check loop with 1.0s wait or instant wake on demand.
3. **Q3 (Workspace verb milestones)**: `workspace.list` and `workspace.show` report `"M1c"` (built in run 2); `workspace.prune` and `workspace.delete` report `"M2"`; `top` reports `milestone: null`.
4. **Q4 (Inbox run tokens)**: External runs get no run token; inbox agents authenticate with their agent token.
5. **Q5 (Task start on launch agent)**: Calling `task start` on an agent with `delivery='launch'` returns rc 6 `INVALID_TRANSITION` (`details.reason: "launch_mode"`).
6. **Q6 (Lead workspace directories)**: Lead workspace directory created at P1 (`intake submit`) and `set-lead`.
7. **Q7 (Commits count)**: Every commit counts towards milestone deliverables.
8. **Fail reason word**: `task.fail` stores `failure_json = {"reason": "reported", "details": {"message": reason, "run_id": run_id}}`.
9. **Heartbeat without event**: `task heartbeat` updates `leases.expires_at` and `leases.heartbeat_at` without recording events to prevent event spam.
10. **Rework feedback source**: Rejection feedback for subsequent attempts is retrieved from the latest `task.rejected` event payload (`reason`) plus the previous deliverable summary, requiring no schema changes. `tasks.deliverable_json` is the wrapper `{"deliverable": {...}, "submitted_by": ..., "submitted_at": ...}`, so the summary is `deliverable_json.deliverable.summary`; `_format_task` unwraps `deliverable` for `task show`. Run 2's prompt renderer must read that path.
11. **Fencing & Stale Write Precedence (§11.5)**: On all four fenced writes (`heartbeat`, `progress`, `submit`, `fail`), lease and fence validation runs after authz. `submit` and `fail` then check `status == "running"` after the fence check, returning rc 9 `LEASE_EXPIRED` or `FENCE_MISMATCH` for a stale write; `heartbeat` and `progress` have no status check. `task.fail` was fixed in `77ce885` (in run 1 it returned rc 6 `INVALID_TRANSITION` for stale writes).
12. **Agent-token fenced writes on a launch-mode assignee (77ce885)**: `task.heartbeat/progress/submit/fail` by an agent token whose task assignee is not `delivery='inbox'` returns rc 4 `NOT_ASSIGNEE` with `details.delivery` and `details.required_delivery: "inbox"` (plan §7.2/§7.3). `task.start` on a launch task stays rc 6 `INVALID_TRANSITION` with `details.reason: "launch_mode"` (Q5).
13. **G12 lockout window (562e364)**: Root cause, quoted verbatim from the commit message: "the testing-mode passphrase lockout window was a hard-coded 2 s. The five failing attempts take ~1.0 s unloaded, but under the parallel runner each bin/mesh subprocess (interpreter start + scrypt KDF) can take 3-5x longer, so the first failure aged out of the window before the fifth landed and the sixth attempt returned PASSPHRASE_INVALID instead of LOCKED_OUT." The fix adds the testing-only setting `auth.lockout_window_s` (range 1..3600, rejected outside testing mode, not in plan §5.7) and `test_passphrase_lockout` sets it to 10 s and polls for the lockout to lapse. Production stays at 300 s. The real effective testing default: testing mode is 300 s unless a test sets `auth.lockout_window_s`; the 2 s fallback applies only to homes without that settings row (see §1.9).

#### ACC Coverage in Run 1
- **A6** (Full): `tests/unit/test_gitops.py::TestGitopsAllowlist::test_gitops_allowlist`, `test_no_git_subprocesses_outside_gitops`
- **D14** (Full): `tests/integration/test_execution_inbox.py::TestExecutionInbox::test_d14_inbox_concurrency_limit`
- **E11** (Full): `tests/integration/test_execution_inbox.py::TestExecutionInbox::test_e11_lead_transfer_authority_on_task_create_and_accept`
- **E15** (Full): `tests/integration/test_execution_inbox.py::TestExecutionInbox::test_e15_completion_blocked_on_submitted_task_and_pending_approval`
- **E16** (Full): `tests/integration/test_execution_inbox.py::TestExecutionInbox::test_e16_wait_until_completed_and_completion_notifications`
- **F5** (Full): `tests/integration/test_execution_inbox.py::TestExecutionInbox::test_f5_dependency_promotes_on_accept_and_cycle_rejected`
- **F7** (Full): `tests/integration/test_execution_inbox.py::TestExecutionInbox::test_f7_cli_accept_running_task_rejected`, `tests/unit/test_states.py`
- **F11** (Full): `tests/integration/test_execution_inbox.py::TestExecutionInbox::test_f11_rejected_task_history_and_attempt_count`
- **G4** (Full): `tests/integration/test_execution_inbox.py::TestExecutionInbox::test_g4_lead_actions_leave_no_approval_rows`
- **G16** (Full): `tests/integration/test_execution_inbox.py::TestExecutionInbox::test_g16_executor_notes_cannot_bypass_approval_gates`; supporting: `tests/integration/test_submit_cli.py` (the `--deliverable` + `--summary` merge and the explicit-JSON-`summary`-wins case)
- **H4** (Full): `tests/integration/test_execution_inbox.py::TestExecutionInbox::test_h4_fence_mismatch_and_recovery`, `tests/integration/test_execution_inbox.py::TestExecutionInbox::test_h4_stale_task_fail_reports_lease_expired_then_fence_mismatch`, `tests/unit/test_leases.py`
- **H6** (Full): `tests/integration/test_execution_inbox.py::TestExecutionInbox::test_h6_inbox_lease_expiry_and_heartbeat`
- **H7** (Full): `tests/integration/test_execution_inbox.py::TestExecutionInbox::test_h7_inbox_no_response`
- **H15** (Full): `tests/integration/test_execution_inbox.py::TestExecutionInbox::test_h15_workspace_fresh_vs_reused_after_lease_expired` (also asserts the `task.retried` payloads: `workspace: "fresh"` with the new path, `workspace: "reused"` with the unchanged path)
- **J3** (Partial - replay clause): `tests/integration/test_execution_inbox.py::TestExecutionInbox::test_j3_idempotent_submit_replay`
- **A8** (Ongoing invariant check): `tests/integration/test_execution_inbox.py::TestExecutionInbox::test_a8_doctor_check_invariants_while_running`

#### Remaining Commands Reporting "M1c"
Commands returning rc 2 `NOT_IMPLEMENTED_IN_M1` with milestone `"M1c"` (verified via `grep -rn '"M1c"' mesh/`):
- `task log` (`task.log`)
- `run list` (`run.list`)
- `run show` (`run.show`)
- `run log` (`run.log`)
- `workspace list` (`workspace.list`)
- `workspace show` (`workspace.show`)

#### Setup Changes on Existing M1a/M1b Tests
No assertion was removed or weakened. The setup changes are:
- `tests/integration/test_daemon.py::test_passphrase_lockout` sets `auth.lockout_window_s=10` and polls for the lockout to lapse instead of `sleep(2.1)` (G12 flake fix, `562e364`).
- `tests/integration/test_approvals.py::test_g15_testing_mode_expiry` polls for `status == "expired"` instead of `sleep(2.1)`/`sleep(2.5)`; every rc 12 `APPROVAL_EXPIRED`, event and outbox assertion is unchanged.
- The two inbox-wait tests in `tests/integration/test_inbox.py` and the two task-wait waits in `tests/integration/test_tasks.py` wait for `active_waits >= 1` instead of `sleep(0.3)`, which is stronger because it proves the waiter registered.
- `tests/integration/test_execution_inbox.py` and `tests/integration/test_projects.py` changed comments only; their sleeps stay, because `project.wait` is not counted in `active_waits`.

#### Testing-Only Setting Outside Plan §5.7 (Recorded Deviation)
- `auth.lockout_window_s` is a testing-only setting: default 300, range 1..3600, and `validate_setting` rejects it outside testing mode (`MESH_TESTING=1`). It is not in plan §5.7, and it is user-visible in `config list`/`config get` in production while `config set` always rejects it there.
- Effective testing default: testing mode is 300 s unless a test sets `auth.lockout_window_s`; the 2 s fallback in `get_lockout_window_s` applies only to homes without that settings row (homes created before `562e364`).
- Production is unchanged at 300 s (plan §7.1). `test_lockout_window_setting_is_testing_only` covers the rejection outside testing mode and the range.

#### Final Test Suite Metrics
- Counts at `c84d6d0`: unit 113, integration 144, recovery 1, total 258 tests passing.
- Counts on `wt/m1c-notes` (after the two new `test_submit_cli.py` tests): unit 113, integration 146, recovery 1, total 260 tests passing.
- Counts on main after merging `wt/m1c-notes` (`46235cc`): unit 113, integration 146, recovery 1, total 260 tests passing.
- Wall times (honest, host-load dependent):
  - 587 s at `7cf35e7` with 3 workers (review m1c-run1).
  - 555 s after `c84d6d0` with 4 workers (review m1c-run1b): unit 113 in 29.917 s, integration 144 in 524.646 s, recovery 1 in 0.040 s.
  - 432 s on `wt/m1c-notes` with 4 workers (review m1c-notes): unit 113, integration 146, recovery 1.
  - 5m41s (341 s) on main after the `46235cc` merge (coordinator's run): unit 113, integration 146, recovery 1.
  - Quest's 9m47s run at `ef3cc65`.
  - 338 s for the M1.9 docs worktree run on the same code.
- The ≤ 7 min target was **not** met; the result varies with host load by more than 200 s on the same code. The A1 requirement (< 10 min) holds.
- Tripwire files: both `/tmp/mesh-tripwire-hit.log` and `$TRIPWIRE_FILE` empty.

---

## M1.9 Docs (parallel branch)

Parallel branch `wt/m19-docs`, sub-step 2 of run 5 of `docs/coordination/plans/m1c-plan.md`. Items: **G23, G29, J7, N9, N17**. Files created: `docs/usage.md`, `docs/adapters.md`, `tests/unit/test_docs_m19.py`. `mesh/cli/help.py` was checked and **not changed** (it already carries every required sentence).

### Deliverables and ACC mapping
| Item | Where | Test |
|---|---|---|
| G23 | `docs/usage.md` §3 (approvals) and §5; `mesh help approvals`; `mesh help auth`; `docs/usage.md` contains `Mesh does not sandbox external CLIs` | `test_g23_relayed_approvals_and_no_sandbox_sentence` |
| G29 | `docs/usage.md` §5; `mesh help auth` | `test_g29_same_uid_limits_and_no_false_claims` |
| J7 | `docs/usage.md` §4 delivery table + "Mesh cannot push into an interactive TUI" | `test_j7_delivery_table_and_tui_limit` |
| N9 | `docs/adapters.md` six-CLI status table, one status word per CLI; hermes/openclaw `not_installed`; fake is not vendor support | `test_n9_status_table_for_six_vendor_clis` |
| N17 | `docs/adapters.md` "inbox participation, not adapter support" section | `test_n17_inbox_participation_is_not_adapter_support` |

### Help-text check (no edit needed)
- Ran `./bin/mesh --home /tmp/mh-helpcheck help auth` and `... help approvals` (both rc 0, JSON envelope).
- `help approvals` already says "Mesh cannot verify a relayed statement and advises direct_only for sensitive categories".
- `help auth` already contains `Mesh is not a security sandbox`, `Mesh does not sandbox external CLIs`, the same-uid credential/relayed-approval sentence, the passphrase/token-file sentence, the code/K_user capture sentence, the read-only-install recommendation, and the explicit "does not claim" sentence. `TOPIC TEXT ONLY` rule: no change made.

### Readings made
- `docs/acceptance-checklist.md`: §0 notation and the Check/Expect of G23, G29, J7, N9, N17 (verbatim); §0.1 gate table (M1c).
- `docs/plan.md`: §14.2 status table and profile mapping, §14.1 status-word definitions, §16 M1.9 (line 1300, docs only), §7 security model (§7.1 credentials, §7.4 approvals/MAC, §7.5 relayed trust, §7.6 threat model, §7.7 gates vs CLI permissions), §12 notifications/delivery (§12.3 table, "cannot inject text into a running interactive TUI").
- `docs/requirements.md`: §0 scope, §2 adapters (six CLIs, mandatory honesty rule: fake is never vendor support), §3 approvals and "do not claim Mesh sandboxes arbitrary external CLIs", §7 notifications.
- `docs/coordination/plans/m1c-plan.md`: run 5 scope and sub-step 2 wording.
- `mesh/cli/help.py`: `TOPIC TEXT` for `auth` and `approvals`; `mesh/cli/commands/top.py` `handle_help`; `bin/mesh` (help is local, no daemon).
- `mesh/adapters/registry.py`, `base.py`, `fake.py`, `none_adapter.py`, `hermes.py`, `openclaw.py`; grep of `mesh/adapters/` for `launch_enabled`/`launch_milestone`/`status=`.
- `scripts/test.sh`, `tests/unit/test_gitops.py`, `tests/helpers.py` (unit-test conventions); grep `tests/` for existing `ACC: G23|G29|J7|N9|N17` tags (none existed).
- Globbed for `mesh/runwrap.py`, `mesh/daemon/supervisor.py`, `mesh/daemon/recovery.py`, `mesh/core/redact.py`, `mesh/core/prompts.py`: absent on this branch (only `mesh/core/workspaces.py` exists), so both docs state plainly that launch execution is still being built in M1c.

### Readings and decisions
1. **Status words.** `docs/adapters.md` uses only the four checklist words (`verified`, `partially_verified`, `not_installed`, `unsupported`) and one word per vendor row. The adapter probe code can transiently return internal strings (`unverified`, `error`) when a version/help check fails; under normal operation the six rows are four `partially_verified` (droid, agy, kiro, vibe) and two `not_installed` (hermes, openclaw).
2. **Launch state.** Code sets `launch_enabled = False` for all six vendor adapters (droid/agy/kiro/vibe `launch_milestone = "M3"`; hermes/openclaw `"M4"`). `fake` is `launch_enabled = True` but only under `MESH_TESTING=1`. Both docs say launch execution (runwrap/supervisor/recovery) is still being built in M1c and that no vendor CLI can be launched today (rc 13 `ADAPTER_UNSUPPORTED`), so no vendor support is implied.
3. **Honesty phrases.** `docs/usage.md` and `help auth` carry the same five G29 statements; `docs/usage.md` additionally has the explicit combined "does not claim that executors cannot steal credentials, and does not claim that audit verify detects forgeries by a process that modified Mesh's code or captured the passphrase" sentence.
4. **N17 wording.** `docs/adapters.md` says Hermes and OpenClaw can join as `adapter=none, delivery=inbox` and labels that exactly "inbox participation, not adapter support"; their adapter status stays `not_installed` until M4 evidence.
5. **No help.py edit.** The required G23/G29 sentences were already present verbatim, so touching `mesh/cli/help.py` was unnecessary.

### Tests run
- `python3 -m unittest tests.unit.test_docs_m19 -v` — 5 tests, rc 0.
- `bash scripts/test.sh` — see the gate run in the final report (rc 0, tripwire empty).

### Review fixes (m1c-docs, after commit 666962e)

Addressed the Kiro review of commit 666962e. `docs/usage.md` §2.3 and §5 now say plainly that issuing and injecting run tokens is M1c launch execution and is not built yet: the token format, hashed storage, and the authz/run-session checks exist, but meshd issues no run tokens because no launched runs exist; the injection, expiry (+300 s), and revocation sentences are future tense and labelled "(M1c, not yet built)". The §4 delivery table has a per-row Status column: launch executor "being built in M1c (the `fake` test double only); vendor CLIs from M3", launch lead "M2 (plan §12.4); until then use a `delivery=inbox` lead", inbox and user "available now"; the J7 phrases and "Mesh cannot push into an interactive TUI" are unchanged. §1 limits help-verified/`launch_milestone: "M3"` to `droid`/`agy`/`kiro`/`vibe` and notes `hermes`/`openclaw` are not installed with M4. §3.1 marks `delete` and `deliver_to_base` "(M2)". `docs/adapters.md` now says a launch task on `droid`/`agy`/`kiro`/`vibe` returns rc 13 `ADAPTER_UNSUPPORTED` (or `PROFILE_UNSUPPORTED` first if the profile is unsupported), while `hermes`/`openclaw` agents cannot be created (`ADAPTER_NOT_INSTALLED`); the agy note now records that the installed binary is 1.5.3 and its capabilities show `stale` against the 1.5.2 fixtures while the status stays `partially_verified`. `tests/unit/test_docs_m19.py` now uses row-specific J7 assertions plus the launch-lead M2 status, adds case-insensitive `assertNotIn` contrary-claim checks (skipping the disclaimer sentence), and cross-checks each N9 row's milestone against `mesh.adapters.registry`; every `ACC:` docstring tag is preserved. Finding 4 (hermes/openclaw probe status after install) is out of scope for this branch, per the review.

Full-suite result after the fixes:
- `python3 -m unittest tests.unit.test_docs_m19 -v` — 5 tests, OK, rc 0 (0.360 s).
- `bash scripts/test.sh` — rc 0.
  - Unit: 113 tests, OK, 8.821 s.
  - Integration: 144 tests, OK, 329.485 s.
  - Recovery: 1 test, OK, 0.042 s.
  - Total wall time: 338 s.
- Tripwire: `/tmp/mesh-tripwire-suite.log` and `/tmp/mesh-tripwire-hit.log` are both absent (empty) after the suite.
