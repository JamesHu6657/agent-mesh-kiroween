# Implementation notes — m2-r9 (m2-leadturns): lead turns

Run: `wt/m2-r9`, branch `wt/m2-r9`. Items: **L1, L3, L6** (the "stay pending" clause in a
real turn), **K13**. Coordinator decisions applied: Q8 (trigger rule), Q9 (testing-only
backoff), Q11 (fake spec in `--goal`).

Parallel run r7 owns `mesh/daemon/recovery.py`, `mesh/daemon/merge.py` and
`tests/recovery/test_recovery_merge.py`. **None of those files was touched.**

## What was built

### 1. Lead run tokens and lead-turn authz

- `mesh/core/authz.py`: a new branch at the top of `check_authz` for a run token whose
  `scope.role == "lead"` (`_check_lead_run_token`). Order:
  1. `scope.lead_assignment_id` must equal the project's current `lead_assignments.id`
     → otherwise rc 9 `LEAD_SUPERSEDED`, **before** every other lead check;
  2. the command must be in `LEAD_RUN_TOKEN_CMDS` (the §7.3 lead column) → else rc 4
     `CAPABILITY_MISSING`;
  3. the resource must be inside `scope.project_id` → else rc 4 `SCOPE_MISMATCH`;
  4. `intake.submit` / `project.create` still need the agent's `intake` capability.
- The executor run-token branch is unchanged (it is now step 3b and still keys on
  `via == "run_token"` without a lead role). The run-session binding in
  `mesh/daemon/server.py` is unchanged; the only server change is that a **lead** run
  token also gets its agent's `capabilities` loaded (the executor branch never reads
  them, so its behaviour is identical).
- `tests/unit/test_projects.py::TestAuthzMatrix::test_authz_lead_run_token_matrix`
  (ACC: L1) adds the matrix rows: the lead column allowed for its own project, the
  user-only rows and the executor-only fenced writes rejected, cross-project and
  cross-task scope rejected, and `LEAD_SUPERSEDED` for a superseded assignment.

**Reading (recorded):** `config.*` / `agent.*` / `audit.*` are rejected at step 2 of
`check_authz`, *before* the lead branch, so a superseded lead run token calling
`config.set` gets rc 4 `CAPABILITY_MISSING`, not rc 9. The plan says `LEAD_SUPERSEDED`
is checked "before the other lead checks"; the user-only rejection is not a lead check,
and §7.3 gives every agent credential rc 4 on those rows. The test pins both.

### 2. Lead-turn scheduler, debounce, backoff and unreachable

New `mesh/daemon/lead_turns.py`:

- `should_start_turn` — the §12.4 trigger: launch-mode lead; project active;
  `leadturn:<prj>` lease free; no active `lead_turn` run of the project; pending
  `lead_wake` messages with `next_attempt_at <= now`; **at least one of a type other
  than `lead.action_failed`** (Q8); the oldest at least `lead.wake_debounce_s` old, or
  ≥ `LEAD_WAKE_BATCH_TRIGGER` (20) pending.
- `build_prompt_input` — builds r2's `LeadPromptInput` from the DB (project, criteria,
  tasks, open approvals, last 100 notes, last 200 events, the pending messages, the
  authority data) with `limit_bytes = <lead adapter>.max_argv_prompt_bytes`.
- `start_lead_turn` — renders with `render_lead_prompt`, then in **one transaction**:
  marks exactly `rendered.included_message_ids` (≤ 50) `delivering` with
  `delivering_run_id`, inserts the `kind='lead_turn'` run with `lead_assignment_id`,
  issues the lead run token (`scope = {role, run_id, project_id, lead_assignment_id}`),
  takes the `leadturn:<prj>` lease (holder `meshd`), and records `lead.turn_started`.
  Effect: for a repo project, refresh `lead/<agent>/repo` when it is clean
  (`worktree remove` + `worktree add --detach`; dirty → keep and log, K13); write
  `prompt.md`; spawn through the M1c wrapper in `lead/<agent>/` with profile
  `lead.profile` and deadline `lead.turn_timeout_s`.
- `finish_lead_turn` — exit 0 → messages `acked`; otherwise `pending` with
  `attempts += 1` and `next_attempt_at = now + backoff[attempts-1]`; at
  `attempts >= outbox.max_attempts` → `dead` plus one `lead.unreachable` event (whose
  §12.2 fan-out reaches the user and the origin agent). Then the run is closed, the
  token revoked and the lease released.
- `lead_turn_termination_reason` — only the deadline; a lead turn has no task, so no
  task status, post-submit grace or idle timeout applies.

`mesh/daemon/scheduler.py`: `run_scheduler_tick` calls `start_lead_turns_tick` after
the task starts (so a turn sees this pass's events). It honours the existing
`run/launch_hold` testing hook.

`mesh/daemon/supervisor.py`:
- `build_lead_child_env` — the wrapper env without `MESH_TASK_ID`/`MESH_FENCE`.
- `register_wrapper` — the lead spawn keeps its Popen object in `_WRAPPERS`.
- `watch_launch_runs` now selects `kind IN ('task', 'lead_turn')`; `_watch_one`
  branches to `_watch_lead_turn`, which never reaches T7/T8 (m2-plan risk 5).
- `_finalize_termination` branches to `_finalize_lead_turn_termination` for a lead
  turn (its messages go back to `pending` with the backoff, as for a non-zero exit).
- `_close_run` now releases the lease **by `run_id`** instead of by
  `task:<task_id>`: a lead turn has no task, so the old key was `task:None` and the
  `leadturn:<prj>` lease leaked (caught by I8 in the first test run).

`mesh/core/settings.py`: `lead.wake_backoff_s` (Q9) — a JSON array of exactly 3
integers, each 0..600, **rejected outside testing mode** exactly like
`auth.lockout_window_s`. The default is `LEAD_WAKE_BACKOFF_S = (30, 120, 600)`, so
production keeps the §12.4 values. `tests/unit/test_settings.py::
test_lead_wake_backoff_setting_is_testing_only` (ACC: L3) pins it.

### 3. Integration tests

`tests/integration/test_lead_turns.py` (5 tests, one harness each, real CLI against a
`/tmp/mh-*` daemon; lead `flead` = fake/launch/`--can-lead`, project `--lead flead`,
`lead.wake_debounce_s=2`, the FAKE spec in `--goal` per Q11):

| Test | ACC | Covers |
|---|---|---|
| `TestLeadTurnDebounce::test_l1_one_turn_per_debounce_window` | L1 | project creation + 3 follow-ups within 1 s → exactly one turn covering all four; all acked with that turn's `delivering_run_id`; prompt written; no second turn; lease released and token revoked; the token scope is `{role:lead, project_id, lead_assignment_id}` |
| `TestLeadTurnDebounce::test_l1_no_overlapping_turns` | L1 | a second batch while a turn runs waits for the lease; after it ends a second turn runs; no two turns of the project overlap in time |
| `TestLeadTurnBackoff::test_l3_three_attempts_then_dead` | L3 | `FAKE {"fail":{}}` + `lead.wake_backoff_s=[1,1,1]` → exactly 3 attempts (each exit 3), messages `dead` with `attempts=3`, exactly one `lead.unreachable` to the user and to `grok`, no 4th turn within 5 s |
| `TestLeadPromptCapInTurn::test_l6_overflow_messages_stay_pending` | L6 | 25 follow-ups of ~20 KB → the turn's `delivering_run_id` rows are a strict subset, the rest stay `pending`, the prompt is ≤ 100 000 bytes and names what it omitted, no `PROMPT_TOO_LONG`, and a later turn carries what the first left behind |
| `TestLeadRepoWorktree::test_k13_lead_repo_detached_during_turn` | K13 | repo project + `FAKE {"sleep":{"seconds":4}}`; during the turn `worktree list --porcelain` shows `lead/<agent>/repo` detached and not equal to any task worktree nor to the integration worktree; still detached after the turn |

## Readings and deviations

- **Q8 applied.** `lead.action_failed` messages are included in a turn but never
  trigger one by themselves (`should_start_turn` requires at least one other type).
- **Q9 applied.** `lead.wake_backoff_s` is testing-only; production stays 30/120/600.
- **Q11 applied.** The lead prompt renders the goal on its own line, so the tests put
  the `FAKE` spec in `--goal`.
- **`delivering_run_id` is kept on `acked`.** It records which turn carried the
  message; clearing it would lose that audit trail. The L6 test relies on it.
- **Lead dir path uses the agent id**, not the agent name
  (`<workspaces.root>/<prj>/lead/<agent_id>/repo`, §10.1). The K13 test resolves the
  id from `lead_assignments`.
- **`_close_run` releases by `run_id`.** A behaviour-preserving generalisation: for a
  task run the lease key is `task:<tsk>` and `leases.run_id` is unique, so the same
  row is deleted.
- **A lead turn that cannot spawn** closes as `spawn_failed` and puts its messages
  back with the backoff (same path as a non-zero exit), so a broken adapter cannot
  wedge the outbox.
- **`lead.turn_started` event** is recorded in the TX (not in the plan's list, but it
  is the only audit trail of which messages a turn carried; it has no outbox fan-out).

## Changed assertions

**None.** No existing assertion was weakened, deleted or changed. The only test files
touched are `tests/unit/test_projects.py` (a new test method appended),
`tests/unit/test_settings.py` (a new test method appended) and the new
`tests/integration/test_lead_turns.py`.

## Suite wall time

- **Before the first change:** `bash scripts/test.sh` rc 0, 245 unit + 236 integration
  + 15 recovery, **1034 s** (17 min 14 s). The box was loaded (the same suite ran in
  ~430 s for r2). This was already **above the A1 10-minute limit before any change of
  this run**.
- **After the last change:** `bash scripts/test.sh` rc 0, **247 unit + 236 integration
  + 15 recovery**, **961 s** (16 min 1 s). Both tripwire files are absent/empty.
  The suite got *faster* than the baseline despite 7 new tests, because the host load
  dropped; the A1 limit is still exceeded on this box for reasons that predate r9.

## Files changed

| File | Change |
|---|---|
| `mesh/core/authz.py` | lead run-token branch (`_check_lead_run_token`, `LEAD_RUN_TOKEN_CMDS`, `LEAD_RUN_TOKEN_TASK_CMDS`, `_lead_superseded`) |
| `mesh/daemon/server.py` | a lead run token also loads its agent's `capabilities` (executor branch unchanged) |
| `mesh/core/settings.py` | `LEAD_WAKE_BACKOFF_S` + the testing-only `lead.wake_backoff_s` |
| `mesh/daemon/lead_turns.py` | **new**: trigger, prompt input, turn start, exit handling, scheduler entry |
| `mesh/daemon/scheduler.py` | `start_lead_turns_tick` called from `run_scheduler_tick` |
| `mesh/daemon/supervisor.py` | `build_lead_child_env`, `register_wrapper`, `_watch_lead_turn`, `_renew_lead_turn_lease`, `_finalize_lead_turn_termination`, `kind='lead_turn'` in the watch pass, `_close_run` releases by `run_id` |
| `tests/unit/test_projects.py` | new `test_authz_lead_run_token_matrix` (ACC: L1) |
| `tests/unit/test_settings.py` | new `test_lead_wake_backoff_setting_is_testing_only` (ACC: L3) |
| `tests/integration/test_lead_turns.py` | **new**: 5 tests (ACC: L1 ×2, L3, L6, K13) |
| `docs/impl-notes-m2-r9.md` | **new**: this file |

`mesh/daemon/recovery.py`, `mesh/daemon/merge.py` and
`tests/recovery/test_recovery_merge.py` (r7's files) were **not** touched.

## Commits

1. `feat(m2): lead run tokens and lead-turn authz (ACC: L1)`
2. `feat(m2): lead-turn scheduler, debounce, backoff and unreachable (ACC: L1, L3)`
3. `test(m2): lead turn integration tests (ACC: L1, L3, L6, K13)`
4. `docs(m2): notes for r9`

## Test commands and results

| Command | rc | Result |
|---|---|---|
| `bash scripts/test.sh` (baseline) | 0 | 245 unit + 236 integration + 15 recovery, 1034 s |
| `python3 -m unittest tests.unit.test_projects tests.unit.test_settings` | 0 | 11 tests |
| `python3 -m unittest tests.integration.test_lead_turns -v` | 0 | 5 tests, 84 s |
| `bash scripts/test.sh` (final) | 0 | 247 unit + 236 integration + 15 recovery, 961 s |

## Not done / open

- **A1 is not met on this box.** The baseline suite took 1034 s before this run
  changed anything, and the final run took 961 s, so the 10-minute limit is exceeded
  by the merged M2 work (r1–r8) plus host load, not by r9. This run adds 7 tests
  (2 unit, 5 integration; ~85 s when the integration file runs alone). Reducing the
  suite is out of scope for r9 (it would mean touching other runs' tests); it is
  reported for the coordinator.
- L2 and L4 are r10's; L5 is r11's. Nothing here executes `mesh-actions`.
- **L1, L3, L6, K13 are all full** for the clauses this run owns. L6's checklist Check
  (`test_lead_prompt_cap`) is r2's unit test; r9 adds the "messages that did not fit
  stay `pending`" integration clause. K13's "detached at creation" clause is r3's;
  r9 adds "during a lead turn, refreshed and still detached".

---

# r9b — fixes for review m2-r9 (VERDICT REVISE)

Review: `logs/raw/review-m2-r9.md`. Findings 1–4 MAJOR, 5–10 minor. All applied except
finding 10 (A1, for Quest). No existing assertion was weakened; the only changed
assertions are the L1 debounce test's own (finding 4 asked for them) and the K13
test's (finding 8 asked for them) — both listed below.

## Findings and the mechanism chosen

### 1 (MAJOR) — a lead run token is confined to `scope.project_id`

Mechanism: **handlers add the scope filter** (option (a) of the review), not an authz
rejection, so a lead turn's `project list`/`task list`/`event list` still work and
simply return only its own project. `mesh/core/authz.py` gains
`lead_run_token_scope(principal, via)`, which returns `scope.project_id` only for
`via == "run_token"` with `scope.role == "lead"` and `None` for every other caller, so
the relay capability can never widen a run token's reads.

- (a) `project.list`, `task.list`, `event.list`/`event.tail`, `run.list` add
  `project_id = scope.project_id` when the helper returns a project, and skip the
  agent/relay filter in that case.
- (b) `LEAD_RUN_TOKEN_TASK_CMDS` now includes `event.list`, `event.tail`, `run.list`,
  `run.show` and `run.log`, so every command that carries a `task_id` is scoped.
- (c) `intake.submit` / `project.create` with a lead run token and no project →
  rc 4 `SCOPE_MISMATCH`; a follow-up to the scope project is still allowed. Reading
  recorded: a lead turn may only follow up on its own project.
- (d) `inbox list`/`wait`/`ack` with a lead run token see and change only rows with
  `project_id = scope.project_id` and `channel <> 'lead_wake'`; a `lead_wake` row (or
  another project's row) is `NOT_FOUND` on ack. Lead-wake rows reach the lead only
  through the turn prompt, so the L6 "stay pending" guarantee survives a lead that
  follows the prompt's `mesh inbox list` pointer.

Tests: `tests/unit/test_projects.py::TestAuthzMatrix::test_authz_lead_run_token_matrix`
gains `check_authz` rows for `project_id=None` on every list command (with and without
`relay`; this proves only that authz admits them, not that each handler confines
them, see r9c N1), for
`event.list`/`event.tail`/`run.*`/`task.log`/`task.wait`/`dep.list`/`workspace.show`
with another project's task, and for `intake.submit`/`project.create` without a
project. Integration:
`tests/integration/test_lead_turns.py::TestLeadRunTokenScope::test_l1_lead_token_confined_to_its_project`
reads `MESH_TOKEN` from `/proc/<wrapper_pid>/environ` (the G18 helper) during a
sleeping turn and asserts the `lead_wake` rows stay `pending` after `inbox list`,
`event list --task <other project's task>` → rc 4 `SCOPE_MISMATCH`, `project list` →
only the scope project, and new intake → rc 4.

### 2 (MAJOR) — a lead turn that exits 0 and leaves an escapee succeeded

`supervisor._finalize_termination`: for `kind == "lead_turn"` with
`t.end_reason == "leftovers"` it now calls `lead_turns.finish_lead_turn(conn, home,
run, exit_info, testing_mode)` inside a transaction, exactly as a task run goes to
`_finish_run` (H17). Exit 0 → messages `acked`, run `exited` with exit_code 0. Timeout
and recovery reasons keep the failure path (`_finalize_lead_turn_termination`).

Test: `TestLeadTurnLeftovers::test_l1_leftover_after_exit_zero_acks` — goal
`FAKE {"steps": [{"spawn_setsid_grandchild": {"ignore_sigterm": true}}, {"succeed": {}}]}`,
`kill.grace_s=2` → `run.leftovers_terminated`, turn `exited`/0, messages `acked` with
attempts 0, no second turn within 3 s. (`spawn_grandchild` keeps the process group, so
it is not a leftover; `spawn_setsid_grandchild` is the escapee the review's intent
needs.)

### 3 (MAJOR) — the lead `repo/` refresh leaves the event loop

The r4 two-phase pattern, in `mesh/daemon/lead_turns.py`:

- `plan_lead_repo_refresh(conn, home, project_id, agent_id, assignment_id)` — DB and
  filesystem reads only.
- `run_lead_repo_refresh(home, plan)` — the git work (`status --porcelain`,
  `worktree remove`, `worktree add --detach`), run on a registered worker thread.
- `_lead_repo_state(home, plan)` — the registry, keyed by
  `(project_id, lead_assignment_id)`, with `LEAD_REPO_PREP_MAX_AGE_S = 10.0` and
  `_evict_lead_repo_prep` for leads that are no longer candidates.
- `start_lead_turns` does DB reads, then either starts the worker and returns (a later
  tick starts the turn) or, when the result is finished, commits the turn.

**Order chosen:** refresh → TX (messages, run, token, lease) → `prompt.md`/`spec.json`
→ spawn → TX B. The refresh therefore happens *before* the run row exists, so a turn
never starts with a stale `repo/`; a failed refresh is logged and never blocks the
turn. `start_lead_turn` (plan + commit in one call) is kept for callers that are not
the tick.

### 4 (MAJOR) — the L1 debounce test is exact

`test_l1_one_turn_per_debounce_window` now calls `harness.hold_launch_starts()` before
the intake, submits the 3 follow-ups, asserts all 4 `lead_wake` rows are `pending`,
then `release_launch_starts()`. It asserts exactly one `lead_turn` run, that all 4 rows
carry its `delivering_run_id` and are `acked`, that
`turn.started_at >= oldest.created_at + lead.wake_debounce_s` (2 s), and that no second
turn starts within 3 s. **Changed assertions (reason: finding 4):** the old
`assertGreaterEqual(len(first_batch), 2)` and the "eventually all acked by any number
of turns" wait are replaced by the exact assertions above.

### 5 (MINOR) — `lead.unreachable` per episode

`mesh/core/events.py`: for `lead.unreachable` the fan-out dedup subject is the run id
(`lead.unreachable:<run>:<recipient>`), so a second dead episode in the same project
reaches the user again. It stays deterministic, so recovery is idempotent. Done here
(not deferred to r10).

Test: `TestLeadTurnBackoff::test_l3_second_episode_reaches_the_user_again` — two dead
episodes give the user two `lead.unreachable` messages with different dedup keys.

### 6 (MINOR) — the backoff read path is testing-only

`lead_wake_backoff_s(conn, testing_mode=False)` returns `LEAD_WAKE_BACKOFF_S` without
reading the DB outside testing mode, like `auth.lockout_window_s` at read time.
`_release_messages` and `finish_lead_turn` take `testing_mode`; the supervisor passes
`_testing_mode()` at all three call sites.

**Reading recorded:** with `outbox.max_attempts = 3` only `backoff[0]` and `backoff[1]`
are ever used (`attempts - 1` ∈ {0, 1} before `dead`), so the 600 s entry is
unreachable at the default bound. It stays in the constant because §12.4 fixes the
sequence and a raised `outbox.max_attempts` would use it.

Test: `tests/unit/test_settings.py::test_lead_wake_backoff_read_path_is_testing_only`.

### 7 (MINOR) — the authority section

`lead_turns._authority` lists only the categories whose stored policy is not
`not_required` (so `deliver_to_base` is gone by default), and the model list covers
every enabled **launch** agent except the project's current lead (E9 forbids only the
current lead as an assignee; the old `can_lead = 0` filter wrongly dropped every
`can_lead=1` agent).

Tests: `tests/unit/test_lead_turns.py` — `test_authority_lists_only_categories_needing_approval`
and `test_build_prompt_input_uses_the_authority_data`.

### 8 (MINOR) — the K13 test can fail on its clauses and tests the refresh

`test_k13_lead_repo_detached_during_turn` now creates a real code task on an `ibx`
inbox agent and `task start`s it, so a task worktree exists; it asserts the lead
`repo/` differs from it and from the integration worktree. It then commits to the
integration worktree with the fixture helper (`identity=True`, cwd under `/tmp/mh-*`),
triggers a second turn with an intake follow-up, and asserts the lead `repo/` HEAD
equals the new integration tip, is still detached and clean, and that the fixture's
remote refs are unchanged. **Changed assertions (reason: finding 8):** the old
"no task worktree path equals it" loop over an empty list is replaced by the real
task-worktree assertions above.

### 9 (MINOR) — corrected figures

The r9 rows above are corrected here: the base had **231** integration tests (not 236;
r9 added 5), the L1 row now describes the exact assertions of finding 4, the K13 row
now includes the refresh to the new integration tip, and the "Production always uses
LEAD_WAKE_BACKOFF_S" claim is now true at read time (finding 6).

### 10 (MINOR, for Quest) — A1

No change beyond keeping the r9b tests lean: one harness per scenario, `wait_for`
polling, testing-mode short timeouts. The new tests add ~2 unit tests and 4 integration
tests (about 60 s of test time).

## r9b test runs

| Command | rc | Result |
|---|---|---|
| `python3 -m unittest tests.unit.test_projects tests.unit.test_settings tests.unit.test_lead_prompt tests.unit.test_lead_actions tests.unit.test_repos` | 0 | 59 tests, 6.2 s |
| `python3 -m unittest tests.unit.test_lead_turns -v` | 0 | 2 tests |
| `python3 -m unittest tests.integration.test_lead_turns -v` | 0 | 8 tests, 98 s |
| `bash scripts/test.sh` (final) | 0 | see the run summary printed by the coder |

## r9b files changed

| File | Change |
|---|---|
| `mesh/core/authz.py` | `lead_run_token_scope`, the widened task-command set, the intake/project-create scope check |
| `mesh/core/events.py` | `lead.unreachable` dedup subject = run id (finding 5) |
| `mesh/daemon/handlers/{inbox,event,project,task,run}.py` | the scope-filter hunks only |
| `mesh/daemon/lead_turns.py` | plan/commit split, the off-loop refresh registry, testing-only backoff, the authority filter |
| `mesh/daemon/supervisor.py` | leftovers → `finish_lead_turn`; `testing_mode` at the three call sites |
| `tests/unit/test_projects.py` | new authz matrix rows |
| `tests/unit/test_settings.py` | the backoff read-path test |
| `tests/unit/test_lead_turns.py` | **new**: the authority unit checks |
| `tests/integration/test_lead_turns.py` | exact L1, leftovers, token scope, second episode, K13 refresh |
| `docs/impl-notes-m2-r9.md` | this section |

## r9b commits

1. `fix(m2): review m2-r9 finding 1 - confine a lead run token to its project (ACC: L1)`
2. `fix(m2): review m2-r9 findings 2-5, 7-8 - leftovers, off-loop refresh, exact L1, per-episode unreachable, authority, K13 (ACC: L1, L3, K13)`
3. `fix(m2): review m2-r9 findings 6-7 - testing-only backoff read path and authority unit checks (ACC: L3, L1)`
4. `docs(m2): r9b notes for review m2-r9`


# r9c — fixes after r9b

## N1 (MAJOR) — `approval.list` and `workspace.list` are confined too

r9b's finding-1 text said the scope filter covers "every list command". It did not:
`handle_approval_list` filtered only by `--project`/`--status` (and skipped the
per-agent filter for a relay-capable principal), and `handle_workspace_list` walked
every project when `--project` was absent. With project B's turn token, `approval
list` returned project A's approval and `workspace list` returned entries for A and B.
The r9b unit rows only proved that `check_authz` admits those commands.

Fix (scope hunks only):

- `handle_approval_list`: when `lead_run_token_scope(principal, via)` returns a
  project, call `check_authz("approval.list", project_id=<--project>)` first (so an
  explicit other project is rc 4 `SCOPE_MISMATCH`, and a superseded lead is rc 9), then
  force the project filter to the scope project. The relay branch cannot widen it:
  the forced filter is in the SQL, before the per-agent filter.
- `handle_workspace_list`: the existing `check_authz` still runs on the explicit
  `--project` (rc 4), then the project is forced to the scope project.
- `LEAD_RUN_TOKEN_SCOPED_READS` stays a documentation constant (nothing consults it at
  runtime, each handler calls `lead_run_token_scope`); its comment now says so instead
  of claiming the set is enforced.

Tests: `tests/unit/test_projects.py::TestAuthzMatrix::test_lead_run_token_approval_and_workspace_list_confined`
(both handlers, with and without `relay`, with and without `--project`, explicit other
project rc 4, and a plain relay agent token still sees everything). Integration: the
extended `test_l1_lead_token_confined_to_its_project` makes `flead` relay-capable
(`agent update`), creates an approval in each project as the user, and asserts that
`approval list` and `workspace list` with the turn token return only the scope
project and that `--project <other>` is rc 4. Both tests fail without the handler
change (checked by reverting it).

Observation, not changed (out of scope): a lead run token cannot call
`approval request` (`CREDENTIAL_NOT_ALLOWED_IN_RUN`, "Executor run tokens cannot
request approvals"), although the §7.3 lead column lists it. The test therefore creates
the approvals as the user.

## N2 (MINOR) — the `inbox list` check was vacuous

During the sleeping turn every project row is `delivering`, so "still pending" proved
nothing. The test now sends a follow-up once the turn is running, asserts at least one
`pending` `lead_wake` row, that it is still `pending` after `inbox list`, and that
`inbox ack <that row id>` with the turn token is rc 5 `NOT_FOUND` and changes no row.

## N3 (NIT) — `_finalize_termination` return value

On the successful leftovers lead-turn path it returned `"killed"` although the run is
closed `exited`. It now returns `"exited"` (or `"lost"` when there is no `exit.json`,
which is what `finish_lead_turn` closes the run as). The only callers test the value
for truthiness.

## r9c files changed

| File | Change |
|---|---|
| `mesh/daemon/handlers/approval.py` | `handle_approval_list` scope hunk |
| `mesh/daemon/handlers/run.py` | `handle_workspace_list` scope hunk |
| `mesh/core/authz.py` | `LEAD_RUN_TOKEN_SCOPED_READS` comment |
| `mesh/daemon/supervisor.py` | N3 return value |
| `tests/unit/test_projects.py` | handler-level confinement test |
| `tests/integration/test_lead_turns.py` | N1 and N2 assertions in the token-scope test |
| `docs/impl-notes-m2-r9.md` | this section, and the finding-1 wording |

## r9c suite wall time

| Run | Result | Wall time |
|---|---|---|
| Before r9c (r9 final, recorded above) | 247 unit + 231+5 integration + 15 recovery | 961 s |
| Before r9c (r9b final) | not recorded in these notes, and not re-run | n/a |
| After r9c | 251 unit + 239 integration + 15 recovery, rc 0 | **661 s** (11 min 1 s; unit 48.5 s, integration 575.9 s, recovery 35.5 s) |

Load: `uptime` reported a load average of 0.00 before and after the run, but the sandbox
(gVisor) does not report real host load, so the figures are only roughly comparable. The
after run is faster than the r9 figure, which was taken on a loaded box; r9c added 1 unit
test and a few seconds to one integration test. A1 (10 minutes) is still exceeded at
661 s.

# r9d — approval ID scope and L1 timing

## NEW-2 (MAJOR) — approval IDs respect the lead run token's project

`approval.show`, `approval.check`, `approval.consume`, and `approval.cancel` now call
`_check_approval_run_scope` immediately after loading the approval row. It calls
`lead_run_token_scope(ctx.principal, ctx.via)` and rejects a differing `project_id`
with rc 4 `SCOPE_MISMATCH`, including approvals with no project.

**Order chosen:** argument validation and missing-row handling stay unchanged. For
an existing row, scope is checked before relay, requester, project-read, hash, and
state checks. Neither leading both projects nor requesting the approval nor holding
`relay` widens a lead run token. Ordinary agent tokens and user credentials retain
their existing authority. The audit found the same missing scope guard in check,
consume, and cancel, so all four handlers share it.

New unit test beside the r9c test:
`TestAuthzMatrix.test_lead_run_token_approval_id_handlers_confined`. Its 24 rejection
cases cover all four handlers with and without relay, requester and user-requested
approvals in another project, and projectless approvals. It also checks unchanged
rows after denial, successful same-project operations, and ordinary agent reads.
Before the fix, all 24 rejection cases failed. The existing L1 integration test now
also checks `approval show <other approval>` for rc 4 `SCOPE_MISMATCH`.

## NEW-1 (MAJOR) — setup does not spend the sleeping turn's token lifetime

The relay grant and other-project approval request run before creating the sleeping
project. The own-project approval needs an existing project ID, so the test holds
launch starts while creating that project and requesting its approval, then releases
the hold. Both approval requests therefore finish before the sleeping turn starts.
The fake sleeps for 30 s instead of 6 s; the closed-turn wait allows 60 s. Token
availability and turn closure use predicate polling, with no added fixed sleeps.
Every existing assertion is retained.

The original L1 test passed normally, but an in-memory diagnostic that polled until
the six-second turn closed during relay setup reproduced rc 3 `TOKEN_REVOKED` at
the subsequent `approval list`. No diagnostic helper files were created.

## NEW-3 — deferred to r10

Lead run tokens still cannot `approval request` (`CREDENTIAL_NOT_ALLOWED_IN_RUN`),
contrary to `docs/plan.md` §7.3:496 and §12.4:860. Deferred to r10 as requested;
the request handler is unchanged.

## r9d validation

Commands below run from the worktree with `TMPDIR=/tmp`; test homes use `/tmp/mh-*`.
The unit scope regression is new; the L1 integration test and other checks already
existed. Wall times include interpreter startup where `time -p` was used.

| Command / diagnostic | rc | Count | Wall time |
|---|---|---|---|
| New unit regression, initial fixture attempt | 1 | 1 test, fixture uniqueness error, corrected before implementation | ≤0.5 s |
| New unit regression, before handler fix | 1 | 1 test, 24 expected failing subtests | ≤0.6 s |
| Original L1 integration test | 0 | 1 test | 14.07 s |
| Original L1 with slow-setup polling diagnostic | 1 | 1 test, expected `TOKEN_REVOKED` reproduction | 16.29 s |
| `python3 -m unittest tests.unit.test_projects -v` | 0 | 9 tests | 0.41 s |
| `python3 -m unittest tests.integration.test_lead_turns tests.integration.test_approvals -v` | 0 | 30 tests | 335.63 s |
| Updated L1 with 45 extra real CLI setup round-trips (5.84 s of injected setup work) | 0 | 1 test | 40.64 s |
| `bash scripts/test.sh` (one final full-suite run) | 0 | 252 unit + 239 integration + 15 recovery = 506 tests | **569.26 s** (runner: 569 s) |

Full-suite tier times: unit 19.145 s, integration 513.086 s, recovery 36.597 s.
Both tripwire checks passed. This run was below A1's 600 s limit; timings on this
host are not a controlled comparison with r9c. The L1 scope test passed in 38.761 s.

Only the four r9d-allowed files changed: `mesh/daemon/handlers/approval.py`,
`tests/unit/test_projects.py`, `tests/integration/test_lead_turns.py`, and these notes.
No existing assertion was weakened or removed.
