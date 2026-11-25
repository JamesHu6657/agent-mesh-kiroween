# Implementation notes — m2-r10 (m2-leadactions): actions and set-lead termination

Run: `wt/m2-r10`, branch `wt/m2-r10`. Items: **L2, L4**. Coordinator decisions applied:
Q8 (a `lead.action_failed` message never starts a turn by itself), Q10 (actions run after
exit 0 and before the run closes), the set-lead `delivering` supersede decision, and the
coordinator additions **A** (vendor lead-turn launch gate) and **B** (`approval.request`
with a lead run token).

## What was built

### 1. Fake `lead_actions` behaviour (`mesh/testing/fake_agent.py`)

`{"lead_actions": {"actions": [...]}}` prints `params["actions"]` as one fenced
`mesh-actions` block (JSON array) and exits 0. Unit premise test:
`tests/unit/test_fake_agent_behaviors.py::test_lead_actions_prints_one_parsable_block`
(the daemon's `parse_actions` reads the block back unchanged).

### 2. Action execution (`mesh/daemon/lead_turns.py`, `mesh/daemon/server.py`, `mesh/core/lead_actions.py`)

- `finish_lead_turn`: on exit 0 (no signal) and **before** `_release_messages` and
  `_close_run` (Q10), `execute_lead_actions` runs inside `SAVEPOINT lead_actions`. An
  unexpected exception rolls the savepoint back and is logged, so a broken action pass
  never wedges the run. The run's `result_json` gains `actions: {ok, failed}`.
- `execute_lead_actions`: `parse_actions` (r2) on the parsed final text
  (`lead_turn_final_text`, the adapter's `parse_final`, raw stdout as fallback). Items run
  in order:
  - an item error from the parser (`cmd_not_allowed`, bad args, …) → failure, nothing
    dispatched; the exit code is the parser's item-error mapping (`cmd_not_allowed` →
    rc 4);
  - a block with more than `MAX_ACTIONS` items → one failure (index `null`), nothing runs;
  - otherwise the item is dispatched through the new `server.dispatch_internal_request`
    as the **lead run principal** (the run token's credential, principal, scope and
    capabilities, built exactly like `authenticate_and_authorize` does), idempotency key
    `act:<run>:<index>`. A missing/revoked/expired token or disabled agent is the rc 3
    error the CLI would get.
- `dispatch_internal_request(ctx, key)`: the handler table (`INTERNAL_REQUEST_HANDLERS`,
  the 14 allowed lead commands) inside `SAVEPOINT internal_request`; idempotency replay
  and store as for a socket request; an error rolls back only that savepoint
  (rc 10 keeps it, as the socket path does). Returns the response envelope.
- `action_request_args` (the "dispatch mapping only" change in `mesh/core/lead_actions.py`):
  maps the short names a lead writes to the handler argument names (`task` → `task_id`;
  `project` → `project_id` for `project.*`; `project_id` → `project` for `task.create`;
  `note` → `text` for `project.note`) and fills in the turn's project when the action
  names none. An explicit other project is kept, so authz still rejects it (rc 4).
- One `lead.action_result` event per item, `via='lead_turn'`, actor = the lead,
  `credential_id` = the run token, payload `{run_id, index, cmd, ok, exit_code,
  error_code}` (+ `message`, `args` on failure).
- `mesh/core/events.py` fan-out row 11: a `lead.action_result` with `ok:false` → one
  `lead.action_failed` message to the project's **current** lead on `lead_wake`
  (`inbox` for an inbox lead), dedup `lead.action_failed:<run>:<index>` (`:block` for a
  block-level failure).

### 3. set-lead terminates the old lead's turn (`mesh/daemon/handlers/project.py`, `mesh/daemon/server.py`)

- Step 3 of `set-lead` now also supersedes the old lead's `delivering` rows of the
  project and re-creates them `pending` for the new lead (coordinator decision), so the
  terminated turn cannot put them back to `pending`.
- Step 3b queues one `terminate_lead_turn` effect per `starting`/`running` `lead_turn`
  run of the **old assignment** (`end_reason = lead_superseded`). After COMMIT,
  `execute_effects` calls `_terminate_lead_turn` → `supervisor._begin_termination`
  (the §11.4 SIGTERM → `kill.grace_s` → SIGKILL path). The run closes `killed` with
  end_reason `lead_superseded` (new end_reason word); its token is revoked on close.
- While the turn is in its grace window the token is still valid but its assignment is
  not current: every call is rc 9 `LEAD_SUPERSEDED` (r9 authz). After the kill it is rc 3
  `TOKEN_REVOKED`. A terminated turn never reaches `execute_lead_actions` (only exit 0
  does), so no action of it runs and no `lead.action_result` exists for it.

### 4. Addition A — vendor lead-turn launch gate

- `lead_turns.lead_launch_check(conn, adapter, testing_mode)` (raises) and
  `lead_launch_allowed(...)` (bool): the AG step-4 check of
  `handlers/task.py::_validate_assignment_guards` — the test double only in testing mode,
  and only a `launch_enabled` adapter; otherwise rc 13 `ADAPTER_UNSUPPORTED` with
  `details.milestone`. `conn` is accepted but unused, for later evidence gates.
- `plan_lead_turn` calls it before anything else. A refused lead starts no run, its
  messages stay `pending`, and one `lead.turn_refused` event is recorded **per lead
  assignment** (`reason: adapter_not_launch_enabled` or `test_double_disabled`).
- `intake submit --lead`, `project create --lead` (same handler) and `project set-lead`
  call the same helper for a `delivery=launch` lead → rc 13. `handlers/agent.py` and
  `handlers/task.py` were not needed.

### 5. Addition B — `approval.request` with a lead run token (`mesh/daemon/handlers/approval.py`, that hunk only)

A lead run token may request approvals for its scope project only: `check_authz`
(`LEAD_SUPERSEDED` first, then `SCOPE_MISMATCH`), and a request naming no project, or a
task of another project, is rc 4 `SCOPE_MISMATCH`. `requested_by` is the lead agent.
Executor run tokens keep rc 4 `CREDENTIAL_NOT_ALLOWED_IN_RUN`.

## Readings and deviations (recorded)

1. **`via` of the dispatched request.** The run plan says "dispatch internally as the
   lead run principal (`via='lead_turn'`) with the same authz". All authz in the handlers
   keys on `via == "run_token"` (`check_authz`, `lead_run_token_scope`, the fenced-write
   checks). To get the *same* authz without touching every handler, the handler sees
   `via='run_token'` (exactly a CLI call with `MESH_TOKEN`), so the handler's own events
   (e.g. `task.assigned`) carry `via='run_token'` and the run token's identity. The
   per-item `lead.action_result` event carries `via='lead_turn'`.
2. **`task.merge` is in the §12.4 allowed list but not internally dispatchable**: its
   handler is the async merge pipeline. An internally dispatched `task.merge` returns
   rc 2 `INVALID_ARGUMENT` (`not_internal`) and is reported as a failed action (→
   `lead.action_failed`), so the lead can retry it from a CLI call. No test exercises it.
3. **Effects queued by an action are not run, only logged.** The actions run inside the
   supervisor's close transaction, where post-commit effects have no hook. None of the
   14 handlers in `INTERNAL_REQUEST_HANDLERS` queues an effect today (a `task.create`
   to a launch agent is started by the scheduler tick, as from the CLI); the log line
   guards a future handler that does.
4. **`approval.request` with a lead run token and no project/task named**: the action
   mapping fills in the turn's project; a direct CLI call naming none is rc 4
   `SCOPE_MISMATCH` (most conservative reading).
5. **`lead.turn_refused` once per assignment**, not per tick: the scheduler retries every
   tick, and the event is the operator signal, not a log line.
6. The coordinator's `delivering` supersede is applied to all old-lead rows of the
   project in `delivering` (they can only belong to the old lead's turn).

## Changed assertions

None. No existing assertion was changed or weakened.

## ACC coverage

| ID | Status | Tests |
|---|---|---|
| L2 | full | `tests/integration/test_lead_actions.py::TestLeadActions::test_l2_actions_executed_as_lead_and_failures_reach_next_turn` (real CLI + fake lead turn: `task.create` to `fx`, `config.set`, `approval.request`, `task.accept tsk_missing`; Q8 check; follow-up turn prompt contains `lead.action_failed`); partial premises in `tests/unit/test_projects.py::TestAuthzMatrix::test_lead_run_token_approval_request_own_project_only` and `tests/unit/test_fake_agent_behaviors.py::test_lead_actions_prints_one_parsable_block` |
| L4 | full | `tests/integration/test_lead_actions.py::TestLeadSupersede::test_l4_set_lead_terminates_turn_and_supersedes_token` |
| N4 | partial (addition A only) | `tests/integration/test_lead_actions.py::TestLeadLaunchGate::test_vendor_launch_lead_refused` |

## Files changed

- `mesh/testing/fake_agent.py`
- `mesh/daemon/lead_turns.py`
- `mesh/core/lead_actions.py`
- `mesh/daemon/server.py`
- `mesh/daemon/handlers/project.py`
- `mesh/daemon/handlers/approval.py` (the lead-run-token hunk only)
- `mesh/core/events.py`
- `tests/integration/test_lead_actions.py` (new)
- `tests/unit/test_projects.py` (new test only)
- `tests/unit/test_fake_agent_behaviors.py` (new test only)
- `docs/impl-notes-m2-r10.md` (this file)

## Commits

The code for sub-steps 1, 2 and the additions shares files (`lead_turns.py`,
`server.py`, `project.py`) and was written together; each full suite run takes about
9–16 minutes on this host. The plan's sub-steps 1 and 2 are therefore **one** feat commit:
splitting `server.py`/`project.py` by hunk needs interactive staging (not available),
and a by-file split would commit an intermediate code state no suite run ever checked.
With one code commit followed by the (purely additive) test commit, every committed code
state is exactly the code the final suite run passed on.

1. `feat(m2): lead actions, set-lead turn termination, lead launch gate (ACC: L2, L4)`
   (sub-steps 1 + 2 and additions A, B)
2. `test(m2): lead action and supersede tests (ACC: L2, L4)`
3. `docs(m2): notes for r10`

## Suite wall time

| When | Command | rc | Unit | Integration | Recovery | Total wall time |
|---|---|---|---|---|---|---|
| Before any change | `bash scripts/test.sh` | 0 | 266 | 247 | 17 | 947 s |
| After the last change | `bash scripts/test.sh` | 0 | 268 | 250 | 17 | 523 s |

The baseline's 947 s was over the 600 s budget on an unchanged tree (r9d measured
569 s); the host was loaded at that time. The final run is under budget. Both tripwire
files were empty (the runner exits 1 otherwise).

New test time: the three integration tests take about 31 s together run serially
(L2 ≈ 11 s, L4 and the launch gate the rest); none is `@serial`.

---

# Fix run m2-r10b (review `logs/raw/review-m2-r10.md`)

Items: review findings 1, 4, 5, 6 (the "Fix prompt notes" 1-6), finding 3 (Quest: YES,
approval on action items) and finding 2 (Quest: option b, async merge).

## Durable supersede backstop (finding 1)

`lead_turn_termination_reason` now returns `lead_superseded` when the run's
`lead_assignment_id` is not the project's current assignment (or the project has none),
checked before the deadline (`timeout`). The supervisor watch pass is therefore the
durable backstop: after a meshd restart (recovery adopts the live turn) or when the
set-lead effect did not start the termination, the next watch pass terminates the
superseded turn with end_reason `lead_superseded`. The set-lead effect stays for
immediacy; `_watch_lead_turn` asks for a reason only when no termination is registered,
so the two never start a second termination. The `server.py` comment now says this.
Unit test: `tests/unit/test_lead_supersede.py` (4 cases, DB only).

## Request-id correlation (finding 5)

`lead.action_result` carries the dispatched request's id in the event's `request_id`
column and in the payload (`request_id`); the handler's own events (e.g. `task.assigned`)
carry the same id. Item errors and block-level results dispatch nothing and have `None`.

## Grace change (finding 4)

`TestLeadSupersede` sets `kill.grace_s=10` (was 5); no assertion changed.

## Approval on action items (finding 3)

An action item may carry an optional top-level `approval` (next to `args`), the action
form of `--approval`. `parse_actions` keeps it on `Action.approval` (a non-string value is
`invalid_item`; an empty string is no approval, as on the socket). `execute_lead_actions`
sets it as `ctx.approval` of the internal `HandlerContext`, and `dispatch_internal_request`
hashes it into the request hash exactly as `handle_client` does. The handlers then apply
their own checks (`check_and_consume_approval`: op hash with the lead as requester, the
project, status, channel), so an approval of another project or another operation fails
with rc 12 `OP_HASH_MISMATCH` and nothing is consumed. The lead prompt's Actions section
mentions the key in one sentence.

Reading: the task text says the gated action "fails rc 10 without it"; the code's
`EXIT_APPROVAL_REQUIRED` (plan §13.2 in `mesh/constants.py`) is **7**, and rc 10 is
`DAEMON_UNAVAILABLE`. The test asserts rc 7 `APPROVAL_REQUIRED`.

## `task.merge` as an action (finding 2, option b)

- `task.merge` stays in `ALLOWED_ACTION_CMDS` and in the prompt.
- In the close TX, `dispatch_internal_request` routes `task.merge` to
  `_queue_internal_merge`: it runs the merge's own TX1 guards
  (`merge.check_merge_request`: task exists, `check_authz` under the lead run principal
  with `via='run_token'`, so project confinement and `LEAD_SUPERSEDED` apply; kind
  `code`; transition; no task lease; merge lease free) read-only in a savepoint that is
  always rolled back. A refusal is a failed action with the CLI's rc (other project →
  rc 4 `SCOPE_MISMATCH`). If allowed, the request queues one `start_task_merge` effect and
  answers rc 0 `{status: "queued"}`; the `lead.action_result` is ok with
  `status: "queued"`. Nothing is stored under the idempotency key (the queued answer is
  not the merge's outcome). An earlier `task.accept` in the same block is visible to the
  check, so `[task.accept, task.merge]` works in one turn.
- `execute_lead_actions` moves the effect into an in-memory queue
  (`lead_turns._QUEUED_MERGES`). `DaemonServer.start_queued_lead_merges` drains it on the
  event loop after every scheduler tick and termination pass, never inside a
  transaction, and starts a merge only if the close TX that queued it committed (its
  `lead.action_result` row exists; one whose TX rolled back is dropped). Each merge runs
  as an asyncio task through `handle_task_merge` (the socket's entry point: TX1, the git
  pipeline in a worker thread, the final TX), with the lead run principal,
  `via='run_token'`, and the action's request id, so `task.merging`/`task.merged` share
  the `lead.action_result`'s request id. `merge.py` is unchanged.
- Outcome: a merge that ran reports through the existing merge events and messages
  (`task.merged`, `merge.failed` to the lead). A merge whose start TX1 refuses it (e.g.
  the lead was superseded, another merge holds the lease, a duplicate merge of the same
  task) records one more `lead.action_result` for the same index with `ok:false`,
  `phase:"merge_start"`, which fans out as a `lead.action_failed` message.
- Limit: the queue is in memory. If meshd dies between the close COMMIT and the merge's
  TX1, the queued merge is lost; the task stays `accepted` and the next lead turn sees it.
  After TX1, the existing merge recovery (§11.7 step 5) applies.
- Test: `TestLeadMergeAction` (accept + merge of an own submitted code task → `merged`;
  a duplicate merge refused at start; another project's task → rc 4).

## Tests added in r10b

- `tests/unit/test_lead_supersede.py::TestLeadTurnTerminationReason::test_durable_supersede_reason`
- `tests/unit/test_lead_actions.py::TestLeadActions::test_optional_approval_key`
- `tests/integration/test_lead_actions.py::TestLeadActionApproval::test_approval_gated_action_needs_a_matching_approval`
- `tests/integration/test_lead_actions.py::TestLeadMergeAction::test_merge_action_queued_then_merged_and_other_project_refused`
- `TestLeadActions` L2 test: one more assertion block (request-id correlation).

## Changed assertions (r10b)

None weakened. `TestLeadSupersede` only changes `kill.grace_s` 5 → 10 (and its docstring).

## Files changed (r10b)

`mesh/daemon/lead_turns.py`, `mesh/daemon/server.py`, `mesh/core/lead_actions.py`,
`mesh/core/lead_prompt.py` (one sentence in the Actions section),
`tests/integration/test_lead_actions.py`, `tests/unit/test_lead_actions.py`,
`tests/unit/test_lead_supersede.py` (new), `docs/impl-notes-m2-r10.md`.

## Suite wall time (r10b)

| When | Command | rc | Unit | Integration | Recovery | Total wall time |
|---|---|---|---|---|---|---|
| After the last code change | `bash scripts/test.sh` | 0 | 270 OK (95.7 s) | 252 OK (1132.0 s) | 17 OK (38.2 s) | 1267 s |

The host was loaded during this run: `test_j6_idle_60s_no_runs` took 94 s, and the two
new integration tests took 36 s and 34 s, against about 18 s for both together run
alone (`python3 -m unittest tests.integration.test_lead_actions`: 5 tests, 63.6 s).
A1 (< 600 s) is not confirmed on this host; Quest should re-measure on an idle host.
Both tripwire files were empty (the runner exits 1 otherwise).
