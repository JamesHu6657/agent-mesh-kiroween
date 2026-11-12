# Implementation Notes — Milestone Gate M1b

Status: Complete (Steps M1.5 Projects, M1.6 Tasks, M1.7 Approvals completed)
Implementer: agy (Gemini 3.8 Flash High)
Date: 2026-11-07

---

## 1. Scope & Status for M1b

Gate M1b covers plan §16 steps M1.5–M1.7. All steps are implemented:
- **M1.5 (Projects)**: Intake submission, acceptance criteria, lead management, project control (`pause`, `resume`, `cancel`), project completion, notes.
- **M1.6 (Tasks)**: Task creation, assignment guards, dependencies, DAG validation, status transitions, scheduler promotions, outbox fan-out, inbox waits, live event tail.
- **M1.7 (Approvals)**: Generic and gated approval requests, policies, decisions (direct and relayed), consumption with caller binding, expiry sweep, `project update` gated by `scope_change`, and `audit verify` with tamper detection.

Checklist items covered in M1b (all 47 gate items per plan §16 and checklist §0.1):
- **B3**: Create one task with flags and one with the same values via `--input -` (JSON on stdin) -> both rc 0.
- **B4**: `--title y` plus stdin `{"title":"x"}`; stdin `{"titel":"x"}` -> rc 2 `DUPLICATE_PARAM`, `UNKNOWN_PARAM`.
- **B5**: `m task create … --idempotency-key k1 [@lead1]` twice, then key `k1` with different title -> rc 0 `idempotent_replay:true`, then rc 6 `IDEMPOTENCY_KEY_REUSED`.
- **B6**: Row counts of `events`, `outbox`, `runs`, `idempotency` before and after `task create --dry-run`, `task assign --dry-run`, and gated op with `--dry-run` -> all counts unchanged.
- **B11**: In M1 build: `m task merge <tsk> [@lead1]`; `m intake submit --repo … [@grok]` -> rc 2 `NOT_IMPLEMENTED_IN_M1` with `details.milestone:"M2"`.
- **B12**: Gated op idempotency flow with `--approval`.
- **D3**: Invalid model on task create -> rc 2 `MODEL_NOT_ALLOWED`.
- **D7**: Effort validation on task create -> rc 2 `EFFORT_NOT_ALLOWED`, `EFFORT_UNSUPPORTED`.
- **D16**: Profile elevated on task create -> rc 7 `APPROVAL_REQUIRED` (`category:"permission_escalation"`), no task created until approved and re-run with `--approval`.
- **E1**: Intake submission creates project and criteria, lead1 default.
- **E2**: Lead override `--lead lead2` at creation/intake.
- **E3**: Missing lead with no default -> rc 2 `NO_LEAD`.
- **E4**: Submit with `--lead ibx` (not can_lead) and disabled lead -> rc 2 `LEAD_NOT_ELIGIBLE`.
- **E5**: `config set lead.default_agent` direct user only.
- **E6**: Intake capabilities enforcement.
- **E7**: Intake submit follow-up `--message` -> lead gets `project.request_received`.
- **E8**: Lead assigns task directly; unprivileged agent receives rc 4 `NOT_PROJECT_LEAD`.
- **E9**: Lead cannot be assignee -> rc 2 `LEAD_CANNOT_BE_ASSIGNEE`.
- **E10**: `project set-lead` handover, lead history, and invalidates old lead's pending approvals.
- **E12**: Relayed `project set-lead` with `--user-statement`.
- **E13**: `set-lead` to agent with active tasks -> rc 6 `LEAD_HAS_TASKS`.
- **E14**: `project complete` with reports.
- **E18**: Complete project -> rc 0, completed; then set-lead, pause, complete, update -> rc 6 PROJECT_NOT_ACTIVE / conflict; terminal projects are closed to state changes (no reopen in M1).
- **F3**: SQLite triggers prevent UPDATE/DELETE on events; untampered DB passes `audit verify`; tampered DB exits 12 `AUDIT_MISMATCH`.
- **F6**: Project dependency gating: task in project B depends on project A completing; B's task stays assigned until A is completed, then scheduler promotes it to queued.
- **G1**: Two pause calls -> same approval_id, one message in user & grok inboxes, project stays active.
- **G2**: Project pause approve and re-run with `--approval`.
- **G3**: Relayed approval by grok and direct user approval; trust verification.
- **G5**: Agent callers on user-only rows (`config.*`, `agent.*`, `agent token.*`, `audit.*`) receive rc 4 `CAPABILITY_MISSING`.
- **G7**: Agent caller without relay capability -> rc 4 `RELAY_NOT_ALLOWED`.
- **G8**: Lead caller without `--relayed` -> rc 4 `CAPABILITY_MISSING`.
- **G9**: Wrong `--op-hash` -> rc 12 `OP_HASH_MISMATCH`.
- **G10**: Requester or lead approving own approval -> rc 4 `SELF_APPROVAL`.
- **G11**: Policy changed to `direct_only` disallows relayed decision -> rc 12 `CHANNEL_NOT_ALLOWED`.
- **G12**: 5 wrong passphrases -> `PASSPHRASE_INVALID`, 6th -> `LOCKED_OUT`; `auth.failed` events logged without secrets.
- **G13**: Approval check negative cases and op-hash mismatch.
- **G14**: Decision idempotency, decision conflict, and double consume.
- **G15**: Testing mode approval TTL and expiry sweep.
- **G19**: Approval consume by non-requester -> rc 4 `NOT_REQUESTER`.
- **G20**: SQLite injection of bogus user_mac on approved approval -> `audit verify` exits 12 `AUDIT_MISMATCH` with approval id.
- **G21**: `approval request` lifecycle for `spend_money` with check and consume.
- **G28**: Permission escalation default policy `direct_only`: relayed -> 12 `CHANNEL_NOT_ALLOWED`, direct -> rc 0 `trust:"direct_verified"`.
- **J2**: `inbox wait` with nothing pending -> rc 8 `WAIT_TIMEOUT` with `data.items == []`.
- **J4**: `inbox list` at-least-once delivery, idempotent `inbox ack`.
- **J8**: Handover supersedes unacked messages and re-creates for new lead.
- **N4**: Launch agent with `agy` adapter -> rc 13 `ADAPTER_UNSUPPORTED` (`milestone:"M3"`), tripwire clean.
- **N5**: Profile unsupported (`dev`/`elevated`) checked before launch enabled -> rc 13 `PROFILE_UNSUPPORTED`.

---

## 2. Authorization Subsystem (§7.1, §7.2, §7.3, §7.5, §7.7)

### 2.1 Caller Identity & Capabilities
The daemon determines caller authority via `mesh.core.authz.check_authz`:
- **Direct User Factor**: Local socket connection without agent run token (or with user capability). Possesses user authority. Can execute all operations directly, including approvals (`mesh approval approve`).
- **Project Lead**: When invoked with `--as <agent_id>` or with agent run token matching `projects.lead_agent_id`. Has lead authority over their project. Subject to approval policies for privileged/destructive actions (`project_control`, `task_mutation`, etc.).
- **Executor Agent**: Runs executing a specific task. Bound to `task_id` and `run_id`. Never allowed to perform user approvals, bypass approval policies, or modify project state.
- **Relayer Agent**: Agent possessing the `relay` capability. Allowed to submit approvals on behalf of the user when `--relayed` is supplied along with `--user-statement <text>`. The statement is validated (non-empty, maximum 4000 chars) and permanently audited on the approval record and event log.
- **Plain Agent**: General agent without special authority. Cannot submit intake without `intake` capability; cannot perform project control actions.

### 2.2 Capability Checks
Capabilities are loaded from the `agents` table. If an agent lacks the required capability for an action (e.g. `intake` for `intake.submit`, or `relay` for `--relayed`), the request is rejected with rc 4 `CAPABILITY_MISSING`.

---

## 3. Approvals Subsystem (§7.4, §10.6, G1–G15)

### 3.1 Gated Operations & Canonical Hashes
Gated operations compute an operation hash `op_hash` via `mesh.core.approvals.compute_op_hash`:
- Non-operational fields (`idempotency_key`, `dry_run`, `approval`, `format`, `if_version`) are stripped.
- The remaining payload is converted to canonical sorted JSON without floats and hashed with SHA-256 (`sha256:<64 hex chars>`).
- Canonical hashing is deterministic and strictly tested across key ordering variations.

### 3.2 Approval Lifecycle
1. **Approval Required**: When a lead agent attempts a gated operation (such as `project pause`, `resume`, or `cancel`) without an approval token, the daemon creates a pending record in `approvals` with `status='pending'` and raises `MeshError(EXIT_APPROVAL_REQUIRED, ...)`. The pending approval details (including `approval_id`, `op_hash`, `category`, and parameters) are returned in the error response.
2. **Approval Approval**: A user factor approves via `mesh approval approve <approval_id>`. Direct user approval generates a user MAC using the daemon's secret in `mesh.db.meta.user_mac_secret`. Relayed approval records `relayed_by`, `user_statement`, and `evidence_ref`.
3. **Approval Consumption**: The lead caller re-executes the command supplying `--approval <approval_id>`. The daemon validates:
   - Approval exists and has `status='approved'`.
   - Approval has not expired (`expires_at > now`).
   - Op hash matches the current command payload.
   - Target project matches.
   - Upon verification, the approval is atomically marked `status='consumed'`, setting `consumed_at` and `consumed_by_run_id`. Approvals cannot be reused.

### 3.3 Old Approval Invalidation on Lead Change
Per §7.5 and checklist E10, when `project set-lead` changes the project lead:
- Any pending approval requested by the outgoing lead for that project is marked `status='cancelled'` with `decision_reason='lead_changed'`.
- This ensures an outgoing lead's pending actions cannot be approved or consumed after handover.

---

## 4. Projects & Intake Design Decisions (§6.1, §6.2, §6.3)

### 4.1 Non-Repo Projects & Milestone Scoping
- In M1, only non-repo projects are supported.
- Supplying `--repo <path>` to `mesh intake submit` or `mesh project create` immediately returns exit code 2 `NOT_IMPLEMENTED_IN_M1` with `details.milestone: "M2"`.

### 4.2 Lead Eligibility & Defaulting
- `mesh config get lead.default_agent` provides the fallback lead when `--lead` is not passed.
- If no default is configured and `--lead` is omitted, creation fails with rc 2 `NO_LEAD`.
- An agent is eligible to lead if and only if:
  1. The agent exists in `agents`.
  2. `agents.is_enabled == 1`. If disabled, fails with rc 2 `LEAD_NOT_ELIGIBLE` and `details.reason: "disabled"`.
  3. `agents.can_lead == 1`. If false, fails with rc 2 `LEAD_NOT_ELIGIBLE` and `details.reason: "not_can_lead"`.

### 4.3 Acceptance Criteria
- Acceptance criteria passed via `--criterion` (or `--criteria` synonym) are stored in the `acceptance_criteria` table with a deterministic ID (`crit_<id>`).
- On `mesh project complete`, the daemon verifies that all criteria for the project are marked `covered=1`. If any criteria remain uncovered, completion is rejected with rc 2 `PROJECT_CRITERIA_UNMET` and a list of uncovered criterion IDs in `details.uncovered`.

### 4.4 Handover Messages
- When `project set-lead` succeeds, a handover message is inserted into `messages` targeting the new lead's outbox/inbox (`to_agent_id = new_lead`, `type = 'system.lead_handover'`), containing the previous lead ID, project ID, and handover notes.

---

## 5. Ambiguities Resolved & Conservative Readings

1. **Transaction Commit on Exit 7 (`APPROVAL_REQUIRED`)**:
   - *Ambiguity*: Plan §13.2 defines exit code 7 as an error exit code for CLI calls requiring approval. However, the pending approval row generated during the call must persist in the database for the user to inspect and approve.
   - *Resolution*: In `mesh/daemon/server.py`, when a command handler raises `MeshError(c.EXIT_APPROVAL_REQUIRED)` outside dry-run mode, the daemon explicitly commits the transaction instead of rolling back, persisting the pending approval and emitting the `approval.requested` event.
2. **Empty Capability Lists**:
   - *Ambiguity*: CLI argument `--capability` could be omitted or supplied. The default capability for newly created agents in schema is `["intake"]`.
   - *Resolution*: If `--capability` is omitted (`None`), daemon applies the default `["intake"]`. If an explicit empty capability list is passed (e.g. `capabilities: []`), the daemon preserves the empty list so agents can be created without intake privileges (required for testing E6).
3. **Database Invariant I4 on Direct SQLite Test Fixtures**:
   - *Ambiguity*: Tests seeding task records directly into SQLite triggered Invariant I4 check failures during harness teardown (`Invariant I4: Task <tsk> has no event`).
   - *Resolution*: Integration tests seeding mock tasks also seed the corresponding `task.assigned` event in `events`, preserving the event log invariant.

## M1.6 Step (2) - Task Operations and Dependencies

1. **Assignment Guards Ordering**:
   - Guards AG 1-6 are strictly evaluated in order: (1) agent exists, enabled, not current lead; (2) adapter installed; (3) profile supported by adapter (evaluated before launch_enabled per plan §6.2 and checklist N5); (4) adapter launch_enabled; (5) model and effort allowed; (6) permission escalation approval check.
   - For `delivery=inbox`, steps 2-4 are skipped as planned.

2. **Task State & Transitions**:
   - Initial status is `created` when no assignee is specified (T1), or `assigned` when assignee is given (T2).
   - `task.assign` transitions `created` -> `assigned` (T3).
   - `task.cancel` requires non-empty `--reason` and transitions non-terminal tasks to `cancelled` (T22), emitting `task.cancelled` and waking blocked dependent tasks with `task.blocked`.

3. **Dependency Cycle Detection**:
   - `dep.add` runs a cycle check (DFS); self-dependencies and cyclic graphs return rc 2 `DEP_CYCLE`.

## M1.6 Step (3) - Scheduler Promotion and Queue Ordering

1. **Promotion Guards (T4, T13)**:
   - Tasks in `assigned` or `rework` are promoted to `queued` only when:
     - `assignee_agent_id` is non-null.
     - Associated project status is `active`.
     - All task dependencies in `task_deps` are in terminal/accepted states (`accepted` or `merged`).
     - All project dependencies in `project_deps` are in `completed` status.
   - When promoted, `queued_at` is set, `task.queued` event is recorded. For inbox agents, `offered_at` is set, `task.offered` event is recorded, and an outbox record is queued.

2. **Queue Ordering and Blocked Reasons**:
   - Queued tasks are ordered per agent by `priority DESC, queued_at ASC, id ASC`.
   - `queue_position` is the 1-based index in this agent-specific queue.
   - `blocked_reason` reflects:
     - `project_paused` if the parent project is paused.
     - `waiting_for_start` for inbox-mode agents waiting for `task start`.
     - `concurrency_limit` for launch-mode agents.

## M1.6 Step (4) - Events, Outbox Fan-out, and Event List/Tail

1. **Automatic Outbox Fan-out**:
   - `record_event` in `mesh/core/events.py` automatically fans out outbox messages per Table 12.2 in the exact same database transaction as the event.
   - Dedup keys ensure idempotent insertion with `INSERT ... ON CONFLICT(dedup_key) DO NOTHING`.
   - Handover re-creates unacked pending or delivered messages for the new lead and supersedes the old ones (checklist J8).
   - Fan-out errors bubble up to caller to prevent partial/inconsistent event states.

2. **Event Tail**:
   - `mesh event tail` outputs event objects as NDJSON lines.
   - Tail is currently implemented as a one-shot batch for past events; live streaming/long-polling comes with sub-step (5).
   - In accordance with plan §13.2, NDJSON output is not wrapped in a single JSON response envelope.

## M1.6 Step (6e) - Review Fixes (Items 1-10)

1. **Relayed Mutations Restricted & Read Scoping (Items 1 & 10)**:
   - Relayed invocations are restricted in `mesh/core/authz.py` to legitimate relayable mutations (`project.set_lead/pause/resume/cancel/update`, `approval.approve/reject`, and authorized intake/reads). All other mutations via `--relayed` return rc 4 `RELAY_NOT_ALLOWED`.
   - Read scoping is enforced consistently across `task.show`, `task.list`, `dep.list`, `event.list`, and `event.tail` (users and leads can read project resources; non-leads receive rc 4 `SCOPE_MISMATCH`).

2. **Atomic Scheduler Promotions & Single Outbox Path (Items 2, 3, 8)**:
   - Promotion status updates, event emission, and outbox insertions run within a single SQLite transaction per candidate. If event recording or outbox fan-out fails, the status update rolls back cleanly.
   - Scheduler and project handlers rely entirely on the single automatic fan-out path in `fan_out_event`, eliminating duplicate outbox inserts.
   - Scheduler tick exceptions and candidate errors log to `meshd.log` without secrets.

3. **Scheduler T6 Failure Guards (Item 9)**:
   - Scheduler tick scans `queued` tasks before promotions.
   - If a queued task's model is no longer in its assignee's allowed models, it transitions to `failed` with reason `model_not_allowed` and details `{"model": ..., "allowed": [...]}`.
   - If a queued task's assignee uses launch delivery and the adapter is not installed, it transitions to `failed` with reason `adapter_unavailable` and details `{"adapter": ...}`.
   - Status update, `failure_json`, version increment, and `task.failed` event (which fans out to the lead's outbox) occur atomically within a single transaction.
   - Payload includes `attempt` and `max_attempts`.
   - Open point for M1.6/M1.7 `task retry`: T6 fires before T5, so `fence_seq` is still 0. When `task retry` (T19) is built, a second T6 failure before start produces dedup key `task.failed:<tsk>:0:<asg>`, where `ON CONFLICT DO NOTHING` drops the duplicate notification to lead. Documented for coordinator decision when T19 is added.

4. **Task Versioning & Terminal States (Items 6 & 7)**:
   - `tasks.version` is incremented (`version = version + 1`) on every task mutation: `task assign`, `task cancel`, scheduler promotion (T4/T13), and scheduler T6 failure.
   - Terminal states are strictly kind-aware: `general` tasks in `accepted` state are terminal and cannot be cancelled (rc 6 `INVALID_TRANSITION`).
   - Assignment guards check dependencies first to avoid orphan approval requests, validate profiles and efforts against valid sets (rejecting profile/effort swaps), and use `task.assign` operation during assignment.

5. **Paused Projects Reading**:
   - Task operations (`task create`, `task assign`, `task cancel`) and dependency operations (`dep add`, `dep remove`) on a `paused` project are allowed.
   - Tasks on paused projects simply remain in their state and are not promoted to `queued` by the scheduler (matching checklist E17).

---

## M1.6 Step (5) - Inbox Subsystem, Waits, and Live Event Tail

1. **Inbox Subsystem (`inbox list`, `inbox wait`, `inbox ack`)**:
   - Strictly scoped to caller's own inbox (`check_authz(..., "inbox.*")`). Executor run tokens are forbidden per matrix §7.3 (rc 4 `CREDENTIAL_NOT_ALLOWED_IN_RUN`).
   - `--user` is permitted only for direct user callers; agent callers passing `--user` receive rc 4 `CAPABILITY_MISSING`.
   - `inbox list` and `inbox wait` mark returned pending messages `delivered` (at-least-once redelivery until acknowledged).
   - `inbox ack` is idempotent (already-acked messages return rc 0). Acking someone else's message returns rc 5 `NOT_FOUND` without leaking existence.
   - `inbox wait` long-polls on the local Unix socket in the daemon without busy loops and without holding SQLite's write lock or blocking other requests. It waits on an `asyncio.Condition` (`server.notify_cond`) notified after each committed transaction and scheduler tick, with periodic 0.5s re-checks. On timeout, returns rc 8 `WAIT_TIMEOUT` with `data: {"items": []}` per checklist J2.

2. **Task and Project Waits (`task wait`, `project wait`)**:
   - `task wait TSK --until submitted|terminal|state:<s> --timeout DUR`: accepts `submitted` (valid value; in M1b times out with rc 8), `terminal` (`merged`, `cancelled`, or general task `accepted`), and `state:<s>`. Same read scoping as `task.show`.
   - `project wait PRJ --until completed|terminal --timeout DUR`: accepts `completed` and `terminal` (`completed` or `cancelled`). Same read scoping as `project.show`.
   - Both wait long-polls operate asynchronously on the daemon's non-blocking condition, exiting with rc 8 on timeout.

3. **Live Event Tail (`event tail`)**:
   - Streams live NDJSON lines over the Unix socket in strictly ascending sequence order.
   - When `--since SEQ` is omitted, tail automatically begins after the latest sequence number in the database at invocation time.
   - Streams continuously until interrupted or until `--timeout DUR` expires. On timeout, exits 0 after printing all arrived events.
   - Applies the same authorization and own-resources scoping as `event.list`.

---

## M1.6 Step (6g) - Review Fixes (Items 1-6) & Final M1.6 Completion

1. **Task Transitions T19–T21 (Finding 1, Option A)**:
   - `task reassign --to Y --reason`: Evaluates AG 1-6 for agent Y. Increments task version (`version = version + 1`), records the transition reason, and emits `task.reassigned`. Transitions `assigned`, `queued`, `failed`, and `escalated` tasks into `assigned`.
   - `task retry [--model] [--effort] [--reason] [--reuse-workspace]`: Transitions `failed` or `escalated` tasks to `queued` (T19). Supports model and effort overrides validated against the assignee's allowed sets.
     - *Attempt Cap & Approval*: If `attempt >= max_attempts`, triggers approval gate `retry_over_limit` (rc 7 `APPROVAL_REQUIRED`), persisting the pending approval with op `task.retry` and notifying the user. Once approved, retry can be re-executed with `--approval <id>`.
     - *Workspace Reuse*: `--reuse-workspace` is accepted and recorded in params (workspace isolation belongs to M2).
   - `task escalate --reason`: Transitions `failed` tasks to `escalated` (T21), emitting `task.escalated` with fan-out to both user and origin agent inboxes per Table 12.2.
   - `task accept` and `task reject` report milestone "M1c" (rc 2 `NOT_IMPLEMENTED_IN_M1`) as they require T7 (`task submit` from an active run).
   - Verbs still reporting "M1b" are deferred to M1.7: `project update` (gated by `scope_change`), approval verbs (`approval request/check/consume/reject/cancel`), and `audit verify`.

2. **Disconnect Handling & Stream Lifecycle (Finding 2)**:
   - `handle_event_tail_live`, `handle_inbox_wait`, `handle_task_wait`, and `handle_project_wait` receive the `StreamReader` and detect client disconnection via `reader.at_eof()` and `writer.is_closing()`.
   - Disconnected live tails exit immediately, terminating coroutines and preventing resource leaks.
   - Waiters exit promptly without marking outbox rows delivered after the client has disconnected.

3. **Wait Defaults and Socket Timeouts (Finding 3)**:
   - `--timeout` is required for `inbox wait`, `task wait`, and `project wait` (missing flag returns rc 2 `INVALID_ARGUMENT`, `param: "timeout"`).
   - Client socket timeout for `*.wait` commands is set to `wait timeout + 15s`.
   - Client catches `socket.timeout` in `send_request` and `stream_cmd`, returning rc 10 `DAEMON_UNAVAILABLE` with a "response timeout" message.
   - `--dry-run` is rejected on long-poll verbs (`inbox wait`, `task wait`, `project wait`, `event tail`) with rc 2 `INVALID_ARGUMENT` (`param: "dry_run"`).
   - `WAIT_TIMEOUT` (rc 8) on `task wait` and `project wait` carries `data: {"items": []}`.
   - `--until submitted` on a task that is already terminal (or becomes terminal while waiting) runs until `--timeout` expires (rc 8 `WAIT_TIMEOUT`), matching the conservative reading of the plan.

4. **Credential Precedence & Enforcement (Finding 4)**:
   - Supplying `--user` together with `--token-file` is rejected at the client boundary with rc 2 `INVALID_ARGUMENT` (`param: "user"`).
   - Otherwise, `--user` precedence over `--token-file` is preserved.
   - Daemon trust boundary enforces that agent credentials cannot specify `user: true` (rc 4 `CAPABILITY_MISSING`).

5. **Test Matrix Alignment (Finding 5)**:
   - Lead-without-relay test cell in `test_projects.py` verifies `lead1` (assigned project lead with `capabilities: ["intake"]` and no relay) can read `task.show`.
   - CLI test verifies unrelated agents receive rc 4 `SCOPE_MISMATCH` on `event tail --project <P> --timeout 1s`.
   - Unused batch `handle_event_tail` and dead dispatch branch removed; unit test exercises `handle_event_tail_live`.
   - CLI test exercises user-inbox `list` and `ack` using `--user`. Unit test covers run token on `inbox.list` -> `CREDENTIAL_NOT_ALLOWED_IN_RUN`.
   - Non-checklist docstring tags (`ACC: task wait`, `ACC: project wait`, `ACC: event tail`) removed.

6. **CLI Extensions (Finding 6)**:
   - `task assign` accepts `--profile`, `--model`, and `--effort` overrides with the same Assignment Guard validations (AG 1-6) as `task create`.

---

## M1.6 Step (6h) - Review Fixes (Findings 1-6)

1. **Dedup Key Discriminator for Repeated Task Transitions (Finding 1, MAJOR)**:
   - *Coordinator-approved deviation from Plan §12.2's Table 12.2 dedup key format*:
     Added the task `version` after transition as a discriminator to outbox dedup keys:
     - `offered:<task_id>:<fence_next>:v<version>`
     - `task.failed:<task_id>:<fence>:<lead_assignment_id>:v<version>`
     - `task.escalated:<task_id>:<recipient_id>:v<version>`
     - *Rationale*: T19–T21 make task transitions repeatable before T5 moves `fence_seq`. Without this discriminator, transitions like reassigning a queued task to another inbox agent, retrying a T6-failed task without changing the model, or escalating after retry drop the second outbox notification under `ON CONFLICT(dedup_key) DO NOTHING`. Replay safety holds because idempotent replay returns the stored response, and event row, task version bump, and outbox insertion share a single atomic transaction.
   - On T20 from `queued`, old assignee's unacked `task.offered` outbox rows for that task are marked `superseded` (as on handover).
   - On retry re-offers for inbox delivery, `offered_at` is set to `now` and updated in `tasks`.

2. **Shared Assignment Guard Step 5 Helper for Retry (Finding 2)**:
   - Extracted Step 5 into `_validate_model_and_effort` helper shared by `_validate_assignment_guards` and `handle_task_retry`.
   - Validates adapter effort capability on retry, returning rc 2 `EFFORT_UNSUPPORTED` (same as D7) for adapters without effort support (e.g. `vibe`).

3. **Verbs Reporting "M1b" for M1.7 (Finding 3)**:
   - `project update` (gated by `scope_change` per plan §7.4) is explicitly tracked for implementation in M1.7 alongside approval verbs (`approval.request`, `approval.reject`, `approval.cancel`, `approval.check`, `approval.consume`) and `audit.verify`.

4. **Approval Op Hash Category Isolation (Finding 4)**:
   - Included `category` in canonical params within `compute_op_hash`.
   - On the creation side, this guarantees distinct op hashes when two gates (e.g. `retry_over_limit` and `permission_escalation`) evaluate for the same operation (`task.reassign`), preventing collision in the `one_open_approval_per_op` unique index. (Note: full end-to-end isolation was completed in step 6i by binding `check_and_consume_approval` to expected gate category on the consumption side).
   - Tested with seeded `attempt = max_attempts`: lead `task retry` -> rc 7 `APPROVAL_REQUIRED` (`category: retry_over_limit`, `op_type: task.retry`), task stays `failed`; user-direct retry -> rc 0, transitions to `queued`.

5. **Wait-Disconnect Handling & Half-Close Behavior (Finding 5)**:
   - Verified via integration test: client terminated during `inbox wait` does not mark subsequent messages delivered (rows remain `pending`).
   - *Half-close note*: A client that half-closes the connection with `shutdown(SHUT_WR)` triggers `reader.at_eof()`, ending long-poll waits immediately with rc 8 `WAIT_TIMEOUT` or empty items. Mesh's standard client keeps the write-half open and does not half-close.

6. **Stale Map Cleanup and Negative Tests (Finding 6)**:
   - Removed implemented commands (`task.reassign`, `task.retry`, `task.escalate`, and other built verbs) from `PLANNED_COMMAND_MILESTONES` in `server.py`.
   - Added CLI tests covering non-lead callers (rc 4 `NOT_PROJECT_LEAD`), empty `--reason` (rc 2 `INVALID_ARGUMENT`), escalate on non-failed task (rc 6 `INVALID_TRANSITION`), and retry from `escalated` (rc 0, transitions to `queued`).

---

## M1.6 Step (6i) - Review Fixes (Findings 1, 3, 4)

1. **Gate Category Binding in Approval Consumption (Finding 1, MAJOR)**:
   - `check_and_consume_approval` requires `category: str` (the expected gate category).
   - Op hash recomputation uses `category=category` (the expected gate category) rather than `row["category"]`.
   - When consuming an approval with a mismatched category, `compute_op_hash` yields a differing hash from `row["op_hash"]`, raising rc 12 `OP_HASH_MISMATCH` with no database state modifications (approval row remains `approved`).
   - The channel policy check is evaluated against `category` (`get_approval_policy(conn, category)`).
   - All call sites supply their expected gate category:
     - `mesh/daemon/handlers/project.py`: `project.set_lead` (`lead_change`), `project.pause/resume/cancel` (`project_control`).
     - `mesh/daemon/handlers/task.py`: `_validate_assignment_guards` (`permission_escalation`), `task.reassign` (`retry_over_limit`), `task.retry` (`retry_over_limit`).
   - *Combined case*: One `--approval` satisfies one gate. A lead `task reassign` that is simultaneously at the attempt cap and above `max_unapproved_profile` cannot currently consume two approvals in one CLI call and must be executed directly by the user (or deferred to M1.7 for two-approval CLI support per coordinator decision).
   - Unit test (a) verifies `check_and_consume_approval` with category mismatch fails `OP_HASH_MISMATCH` and leaves row `approved`.
   - Integration test (b) exercises the full trace with a seeded `attempt = max_attempts` failed task above target agent's `max_unapproved_profile`: lead reassign creates `retry_over_limit` approval `apr1`; user directly approves `apr1`; user retries task to `queued`; lead re-runs reassign with `--approval apr1`, which triggers AG step 6 and fails with rc 12 `OP_HASH_MISMATCH`, leaving `apr1` approved and task queued with original assignee.

2. **Dead-Waiter Synchronization and Control Run (Finding 3)**:
   - Added `active_waits` counter to `MeshDaemonServer` reported in `daemon.status`.
   - `handle_inbox_wait` and `handle_task_wait` increment `server.active_waits` upon entering wait loop and decrement in `finally`.
   - `test_inbox_wait_disconnect_leaves_outbox_pending` polls `daemon status` until `active_waits >= 1` before sending `proc.terminate()`, proving the waiter was connected and actively long-polling in the daemon. Verifies `active_waits` returns to 0 after termination.
   - Added control run verifying a subsequent live `inbox wait` receives the message and marks the outbox row `delivered`.

3. **Supersede Queries Cleanup (Finding 4)**:
   - In `task.reassign` (T20 from `queued`) and `project.set_lead` (handover), restricted the status filter to `('pending', 'delivered')`, dropping `delivering` which cannot validly transition to `superseded` under `states.py`.
   - Dropped the redundant `LIKE 'offered:<tsk>:%'` clause in `task.reassign` in favor of exact JSON extraction `json_extract(payload_json, '$.task_id') = ?`.

4. **M1.7 Carry-Overs Completed in M1.7**:
   - Findings 2 (superseding unacked `task.offered` rows on transitions out of `queued` such as scheduler T6, task cancel T22, task reassign T20, and task retry T19) completed.
   - Finding 5 (unified canonical op-hash source with category binding in `mesh/core/canonical.py`) completed.
   - Finding 6 (`--approval` re-execution CLI test for `retry_over_limit`) completed.
   - M1.7 note (`handle_approval_approve` DB corruption re-hashing per plan §7.4 step 3) completed.

---

## 7. M1.7 Approvals & M1b Gate Completion

### 7.1 Conservative Readings for M1.7
1. **`approval request` Authority Without `--project`**:
   - When `--project` is omitted, an agent caller is permitted to request an approval if and only if it holds the `relay` capability or currently leads at least one project. Otherwise rejected with exit 4 `CAPABILITY_MISSING`.
   - When `--project` is provided, an agent caller must lead that project or hold the `relay` capability. Otherwise rejected with exit 4 `CAPABILITY_MISSING`.
   - Direct user callers may request approvals with or without `--project`.
2. **Notification Fan-out for Projectless Approvals**:
   - For an approval requested without a `--project`, the `approval.requested` event fans out to the user inbox only (`apr_req:<apr_id>:<user_id>`).
   - For an approval requested with `--project`, the event fans out to both the user inbox and the origin agent's inbox (if set).
3. **Exact Operation Document Strings & §7.4 Deviation**:
   - Standard gated commands use `op` strings matching their CLI verb: `project.pause`, `project.resume`, `project.cancel`, `project.set_lead`, `project.update`, `task.create`, `task.assign`, `task.reassign`, `task.retry`.
   - Generic `approval request` commands use `op: "external.<category>"` (e.g. `external.spend_money`, `external.deploy`).
   - *Coordinator-approved deviation from Plan §7.4 op-document format*: `category` is embedded unconditionally as a reserved top-level key in the canonical op document (`"category": <category>`) and stripped from `params`. This guarantees collision-free op hashes across distinct categories even with identical parameter bodies.
4. **Categories and Policies**:
   - All 12 categories from plan §7.4 are supported in the policy engine: `lead_change`, `project_control`, `scope_change`, `permission_escalation`, `retry_over_limit`, `delete`, `deliver_to_base`, `spend_money`, `deploy`, `delete_external`, `external_other`, `config`.
   - Category `config` is fixed `direct_only` and cannot be modified.
   - Category `permission_escalation` defaults to `direct_only`.
   - Category `deliver_to_base` defaults to `not_required` (the only category permitting `not_required`).
   - All other categories default to `direct_or_relayed`.
   - M2 verbs `delete` and `deliver_to_base` remain unwired to operations.

### 7.2 Decision & Consumption Authority Order & Defect Fixes (Plan §7.3, §7.4)
1. **Decide Authority Check Order**:
   - `handle_approval_decide` strictly enforces the plan §7.3 order: authentication and session binding run first, followed by:
     1. Agent caller without `--relayed` -> rc 4 `CAPABILITY_MISSING`.
     2. `--relayed` without `relay` capability or from run token -> rc 4 `RELAY_NOT_ALLOWED`.
     3. Relayer is the approval's requester or current project lead -> rc 4 `SELF_APPROVAL`.
     4. Category policy is `direct_only` -> rc 12 `CHANNEL_NOT_ALLOWED`.
     5. Wrong `--op-hash` -> rc 12 `OP_HASH_MISMATCH`.
     6. State checks: expired (12 `APPROVAL_EXPIRED`), consumed (12 `APPROVAL_CONSUMED`), conflict (6 `DECISION_CONFLICT`), already decided (rc 0 idempotent replay).
2. **Approval Consume Check Order**:
   - `handle_approval_consume` and `check_and_consume_approval` strictly enforce:
     1. Authority: requester only (else rc 4 `NOT_REQUESTER`, G19).
     2. Op-hash match: provided hash matches stored `op_hash` (else rc 12 `OP_HASH_MISMATCH`).
     3. State checks: expired (12 `APPROVAL_EXPIRED`), already consumed (12 `APPROVAL_CONSUMED`), rejected (12 `APPROVAL_REJECTED`), cancelled (12 `APPROVAL_CANCELLED`), invalid status (12 `APPROVAL_INVALID`), invalid `decided_via` (not in `direct`/`relayed` -> 12 `APPROVAL_INVALID`), missing `decided_at` timestamp (12 `APPROVAL_INVALID`), or direct decision missing `user_mac` signature (12 `APPROVAL_INVALID`).
     4. Policy channel check: if `decided_via` is `relayed` and category policy is `direct_only` -> rc 12 `CHANNEL_NOT_ALLOWED`.
     5. Atomic consume: set `status='consumed'`, `consumed_at=now`, `consumed_by=caller_id`, `consumed_request_id=request_id`.
3. **Approval Check Authority**:
   - `handle_approval_check` is restricted strictly to the direct user and the approval's requester (any other agent receives rc 4 `NOT_REQUESTER`).
4. **Re-execution Op-Hash Caller Binding (Plan §7.4 Step 4)**:
   - When a command re-executes with `--approval APR`, op-hash recomputation binds the **caller's principal ID** as `requested_by`. A different caller attempting to re-execute fails with rc 12 `OP_HASH_MISMATCH`.
   - Consumption records `consumed_by = caller_id` and `consumed_request_id = request_id`.
5. **Decision Records & Signatures**:
   - Direct user decisions require `K_user` from passphrase, verify DB integrity by re-hashing stored `op_json` against `op_hash` (mismatch -> rc 12 `OP_HASH_MISMATCH`), and write `user_mac = HMAC-SHA256(K_user, "mesh-approval-v1\n" + id + "\n" + op_hash + "\n" + decision + "\n" + decided_at)`.
   - Relayed decisions record `decided_via='relayed'`, `relayed_by`, `relayer_credential_id`, `user_statement`, `evidence_ref`, and `user_mac=NULL`.
   - `approval cancel` is requester-only (non-requester -> rc 4 `NOT_REQUESTER`); only `pending` approvals can be cancelled (any other state -> rc 6 `INVALID_TRANSITION`).

### 7.3 Expiry Sweep & Background Scheduler
- The background scheduler tick sweeps expired approvals (`expires_at < now` and `status IN ('pending', 'approved')`), atomically transitioning them to `expired`, recording `approval.expired`, and notifying the requester via outbox (`apr_dec:<apr_id>:expired`).

### 7.4 Scope Change & Project Update
- `handle_project_update` implements `project update` gated by `scope_change`. Updates project goal and/or adds acceptance criteria. When called by lead without approval, returns rc 7 `APPROVAL_REQUIRED` (`category: "scope_change"`). Re-running with `--approval` applies the update and consumes the approval.

### 7.5 Audit Verify & Tamper Detection
- `mesh audit verify --user` is direct user only (agent callers -> rc 4 `CAPABILITY_MISSING`).
- Verifies:
  1. Continuity of the SHA-256 event hash chain from `seq=1`, `prev_hash='genesis'` through all event rows.
  2. HMAC-SHA256 signature on all direct approval decisions using `K_user`.
  3. HMAC-SHA256 signature on all direct user events using `K_user`.
- Decision row integrity rules:
  - An approval row is treated as a decision row if `status IN ('approved','rejected','consumed')`, or `decided_via IS NOT NULL`, or `decided_at IS NOT NULL`.
  - For every decision row, `decided_via IN ('direct','relayed')`, non-NULL `decided_at`, and non-NULL `decided_by` are strictly required; otherwise the ID is added to `broken_approval_ids`.
  - For `direct` decision rows, `user_mac` must be present and match HMAC-SHA256 signature over the decision details; missing `decided_at` or `user_mac` is flagged as a mismatch in `broken_approval_ids`.
  - Every relayed decision row is counted in `relayed_summary` regardless of `decided_at`, ensuring all relayed operations remain visible for human auditing per §7.5.
- Any mismatch raises rc 12 `AUDIT_MISMATCH` with details listing broken event seqs and approval IDs.
- Fixed Defect (e): ensured all direct user event writers across the daemon handlers (`agent.*`, `config.*`, `project.*`, `task.*`, `dep.*`, `approval.*`) supply `k_user=ctx.k_user`. Fixed `auth.failed` events to record `actor_kind="system"` so unauthenticated login attempts are not treated as direct user actors.

### 7.6 Testing Mode Lockout Window Deviation (§17.1, §7.1, G12)
- In testing mode (`lockout_window` in `DaemonServer.authenticate_and_authorize` (mesh/daemon/server.py)), the lockout window is 300 s unless a test sets the testing-only setting `auth.lockout_window_s`; the 2 s fallback applies only to homes without that settings row. Production is fixed at 300 s (5 minutes).
- **Reason**: Checklist item G12 requires asserting CLI behavior "after the lockout ends" (`m event list --type auth.failed [U]` returning rc 0 after the lockout expires). A short testing window lets the automated integration test suite verify this transition without blocking the test run for 5 minutes. Since M1c the window is a testing-only setting (`auth.lockout_window_s`, range 1..3600, rejected outside testing mode) so a test can pick a window that stays stable under parallel load; `test_passphrase_lockout` sets 10 s and polls for the lockout to lapse.
- **Production impact**: None. Production mode strictly enforces the standard 300 s (5 minute) lockout window after 5 consecutive passphrase failures per plan §7.1.
