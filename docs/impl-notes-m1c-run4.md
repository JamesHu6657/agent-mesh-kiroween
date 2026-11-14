# M1c run 4 — implementation notes (crash recovery)

Branch `wt/m1c-run4`, based on 3745435 (run 3 HEAD). Scope: plan §11.7 steps 1–4, 6, 7
(step 5 is M2), the §17.1 harness verbs, the `MESH_TEST_CRASH_AT` hooks, and
ACC I1–I8, I10, I11, J3 (recovery clause), J5.

## 1. Commits

| Commit | Sub-step | What |
|---|---|---|
| (see report) | 1 test | harness `kill9_daemon`, `restart_daemon`, `meshd_pid`, `wait_daemon_exit`, `start_daemon(extra_env, ready_timeout, wait_ready)` |
| (see report) | 2 feat | `mesh/daemon/recovery.py`, run on every boot before the socket opens |
| (see report) | 3 feat | `MESH_TEST_CRASH_AT=after_tx_a|after_popen`, spec.json `testing_wrapper_delay_s` |
| (see report) | 4 test | `tests/recovery/test_recovery.py` (11 tests), `tests/unit/test_recovery_units.py` (5) |
| (see report) | 5 docs | these notes |
| (see report) | review | `fix(m1c): review m1c-run3 minors` (§9) |

## 2. Where recovery runs

`DaemonServer.run_server`: storage → flock → boot row (step 1: earlier open boots get
`crashed=1`) → subreaper → **`run_recovery()`** → socket → scheduler and termination loops.
Recovery runs on **every** boot, not only after a crash: a clean `daemon stop` leaves runs
running (I8), and they are adopted the same way. A failing recovery is logged and meshd still
starts (each step is its own transaction, so a half-done recovery is simply resumed next boot).
`refresh_run_caches()` runs after it, so adopted runs count for run-session binding at once.

The longest synchronous part is the 5 s wait for `wrapper.json`; the harness waits up to 30 s
for the socket on restart.

## 3. Step 2: runs held by meshd

For each `starting`/`running` run joined to a `holder_kind='meshd'` lease, in start order,
each in its own transaction(s):

1. **`wrapper.json` present, pid alive (not a zombie), starttime equal → adopt.** Lease holder
   := current boot id (`expires_at`/deadline unchanged), event `run.adopted`
   `{run_id, old_boot_id, new_boot_id, wrapper_pid, deadline_at, from_spawn_window}`. If TX B
   never committed (status `starting` / no `wrapper_pid`), the run gets `running`, `wrapper_pid`,
   `pgid`, `proc_starttime` from `wrapper.json`. A passed deadline starts a `timeout`
   termination at once (R3); the termination loop finishes it.
2. **`exit.json` present → normal exit processing**: the lease holder moves to the new boot and
   the supervisor's own `_watch_one` handles it (leftovers, auto-submit, exit_nonzero,
   exited_after_submit), so there is one code path for exits.
3. **`wrapper.json` present, process dead or starttime differs → `lost_on_restart`.** The dead
   wrapper's pid and pgid are **never** used to find or signal processes (either may have been
   reused by an unrelated session leader). Only processes whose environ carries
   `MESH_RUN_ID=<run>` are terminated (§11.4 escapees, `terminate.begin(run, pgid=None,
   wrapper_pid=None)`); the run closes only after they are gone (release condition). Then run
   `lost`/`lost_on_restart`, T8 `lost_on_restart` (or T10 `cancelled` if the task was
   `cancelling`; a `submitted` task only gets its run closed).
4. **No `wrapper.json`** (crash before or during the spawn): environ scan; if a carrier lives,
   poll 5 s for `wrapper.json` and adopt; otherwise terminate the carriers as in 3 and close
   `spawn_failed`/`lost_before_spawn`, T8 `lost_before_spawn`. No respawn: the task is
   `failed`, and only `task retry`/`reassign` can requeue it (§11.6).

Terminations started by recovery close their run with the recovery status (`lost`,
`spawn_failed`) instead of `killed`: `supervisor.RECOVERY_END_STATUS` maps the end reason, and
`_finalize_termination` uses it. A task in `cancelling` still ends `cancelled`.

Note: TX B commits right after `Popen`, before runwrap writes `wrapper.json`, so a run can be
`running` without `wrapper.json` for a few hundred ms. If meshd and that group die in that
window, recovery correctly classifies it as `lost_before_spawn`. The I2/I3/I8 tests therefore
wait for `wrapper.json` and the fake before killing anything.

## 4. Steps 3, 4, 6, 7

- **3.** Agent-held leases: `expires_at = max(expires_at, now + lease.ttl_s)`, one tx.
- **4.** Outbox `delivering` rows whose `delivering_run_id` is not an adopted run → `pending`,
  `attempts + 1`, `delivering_run_id = NULL`. (Lead-turn runs are M2, so today every
  `delivering` row is requeued.)
- **6.** `cancelling` tasks with no `starting`/`running` run → `cancelled` (event
  `task.cancelled`, payload `recovered: true`, dependents get `task.blocked`). In practice
  step 2 already finishes most of these, since a cancelling task's run is processed there.
- **7.** `sweep_expired_approvals`, then `daemon.recovered` with the counts:
  `runs_adopted`, `runs_adopted_past_deadline`, `runs_exited`, `runs_lost`,
  `runs_spawn_failed`, `runs_terminating`, `run_errors`, `agent_leases_extended`,
  `outbox_requeued`, `tasks_cancelled`, `approvals_expired`, `boot_id`.

**Idempotence (J3).** Every step re-reads current state and guards its UPDATE with the
expected status, and every notification goes through `record_event` with the deterministic
§12.2 dedup keys. A second recovery therefore finds nothing to do for an already-closed run
(test: I2 followed by kill -9 + restart and stop + start: one `task.failed` event, one
message, no duplicate dedup keys).

## 5. Testing hooks (sub-step 3)

- `MESH_TEST_CRASH_AT` is read only when meshd runs with `MESH_TESTING=1`
  (`supervisor._crash_at()`); other values are ignored.
- `after_tx_a`: right after TX A commits, meshd sends itself SIGKILL (the same as `kill -9`:
  no cleanup, the boot row stays open, the socket file stays).
- `after_popen`: meshd writes `testing_wrapper_delay_s: 3.0` into `spec.json` (risk 6: never
  through the environment, which loses all `MESH_*` anyway) and SIGKILLs itself right after
  `Popen`, before TX B. runwrap sleeps that long (accepted only in `0 < d <= 10`) before it
  writes `wrapper.json`.
- The tests hold launch starts while the task is created, because T5 can run inside the
  `task create` request's scheduler wake and the crash would otherwise eat the response.

## 6. Harness (sub-step 1)

- `meshd_pid()`: `run/meshd.pid` is trusted only if `/proc/<pid>/environ` carries this home's
  `MESH_HOME`.
- `kill9_daemon()`: SIGKILL that pid (or the harness's own subprocess), wait until it is gone.
  Runs are not touched.
- `restart_daemon(extra_env=None, ready_timeout=30)`: starts meshd and waits for the socket.
- `start_daemon(extra_env=..., ready_timeout=..., wait_ready=...)`: `extra_env` goes to meshd
  only; readiness fails fast if meshd exits.
- `wait_daemon_exit()` for the crash-hook daemons.

## 7. Tests (tests/recovery/test_recovery.py)

| Test | ACC / R |
|---|---|
| `test_i1_i11_adopt_after_kill9` | I1, I11 (R1, R10): spec exactly as the checklist (record_pid, sleep 8, succeed) |
| `test_i2_j3_lost_on_restart_once` | I2, J3 (R2), plus two more recoveries |
| `test_i3_deadline_passed_while_down` | I3 (R3), `--timeout 3s`, `kill.grace_s=2` |
| `test_i8_daemon_stop_leaves_run_running` | I8 |
| `test_lost_wrapper_leftovers_cancelling_and_outbox` | §11.7 step 2 (lost with a live carrier, cancelling) and step 4 |
| `test_i7_crash_after_tx_a` | I7 (R7), including the 20 s no-respawn wait |
| `test_i10_crash_after_popen_adopts` | I10 (R9) |
| `test_i4_idempotent_burst_across_kill9` | I4 (R4) |
| `test_i5_j3_submit_message_once_across_restarts` | I5 (R5), J3 replay clause |
| `test_i6_inbox_lease_survives_restart` | I6 (R6), `lease.ttl_s=4`, down 3 s, heartbeat after the original expiry |
| `test_j5_never_connected_origin_keeps_messages` | J5 |

The recovery placeholder test is removed; the tier now reports 11 tests.

## 8. Deviations and open points

- **I4 burst and replays run in-process** (`setup_cli`: same argv, socket protocol, auth and
  envelope, no interpreter start per call). 600 subprocess calls would add about 2 minutes to
  the tier. Three replays (first, middle, last key) also go through a separate `bin/mesh`
  process. The kill lands after at least 40 creates; the test asserts that some calls
  failed and some succeeded, so the crash really hit the burst.
- **I4 "replays return idempotent_replay:true"** is asserted for every key whose create
  returned 0 before the crash. Keys whose create failed during the outage are created by the
  first replay; a second full replay then returns `idempotent_replay:true` for all 200.
- **I10:** whether recovery reaches the 5 s `wrapper.json` wait or finds `wrapper.json` already
  written depends on how fast meshd restarts (under 3 s on this box). Both lead to the same
  adoption; `run.adopted.from_spawn_window` is true in either case, because TX B never committed.
- **Lost runs with a pending `task fail`:** if the agent reported a failure and meshd then lost
  the run, recovery records `lost_on_restart` (the plan's wording); the reported message is not
  carried over. Termination-based closes still prefer `reported`.
- **Run tokens of adopted runs** are not re-registered for exact-value redaction in meshd
  (only their hash is stored). runwrap still redacts its own `MESH_TOKEN`, and the pattern
  rules still match `mesh_rt_…`.
- Step 6 is reached directly only when a cancelling task has no active run at boot; the test
  covers the common route, where step 2 closes the run and applies T10.

## 9. Review m1c-run3 minors (one commit)

1. **Pid reuse (first).** `terminate.run_targets(run_id, pgid, wrapper_pid,
   wrapper_starttime, seen)` now returns `{pid: starttime}` and trusts no bare number:
   - every registered meshd child is excluded, except this run's wrapper when the registry
     starttime equals `runs.proc_starttime` (an adopted wrapper is not registered at all);
   - a process is a target if it carries `MESH_RUN_ID=<run>`; a pgrp/session match counts
     only while the group is provably the run's: the wrapper is alive with its stored
     starttime, or the pid was already seen by this termination with the same starttime,
     or its PPid chain reaches such a seen pid (or the live wrapper);
   - the step loop re-signals a known pid only if it qualified again on these rules.
   `Termination` carries `wrapper_starttime` and a real `leftovers_reported` field.
   Wrapper reaping: `_spawn_run` keeps the wrapper `Popen` in `supervisor._WRAPPERS`;
   `_reap_wrapper(run)` calls `poll()` on it (never `waitpid` on a stored number) and
   unregisters only if the pid is still registered as `wrapper`. Adopted wrappers have
   no Popen and nothing is waited on. Residual: once the wrapper is gone, a leftover that
   dropped `MESH_RUN_ID` from its environment and whose parent chain to a seen process is
   broken (reparented to meshd) is no longer found by the pgid number alone. This is the
   tradeoff the review asked for.
   Tests: `TestPidReuseSafety` (registered wrapper-kind pid with another starttime; stale
   pgid member without `MESH_RUN_ID` after the wrapper is gone, plus the seen/descent and
   reused-seen cases; Popen-only reaping).
   Changed test setup (review m1c-run4 §4): the existing
   `TestTerminationStateMachine` escapee test called `terminate.begin(..., proc.pid, None, ...)`;
   it now passes `(..., proc.pid, proc.pid, ..., wrapper_starttime=st)`. Reason: the group is
   trusted only through a live wrapper identity, so the test must give the group leader as
   the wrapper with its starttime. Its assertions are unchanged.
2. **`--cancel-running`.** Behaviour unchanged (Kiro: correct per P5). Help text now says
   running tasks are always cancelled through `cancelling` and the flag is accepted for
   compatibility. The P2 ↔ §13 `pause --cancel-running` inconsistency is recorded as a
   plan question in the run 3 notes §3.
3. **Auto-submit detection** is structural: T7 records `auto: true` in
   `deliverable_json`; `supervisor.is_auto_submit()` checks that (strictly `True`) in the
   post-submit-grace and auto-submitted paths. An agent named `system` now gets the grace
   kill. Unit test `TestTerminationReason.test_agent_named_system_still_gets_post_submit_grace`.
4. **`offered_at`.** Documented in the run 3 notes §4 as "start of the current
   acknowledgement window"; `no_response` details also carry `ack_window_start`. I did not
   add a column (schema change for a naming issue). New test for the `concurrency_limit`
   branch: `test_e17_inbox_no_response_clock_paused_at_concurrency_limit`.
5. **Inbox external run on project cancel** ends `killed(project_cancelled)`; reading
   recorded in the run 3 notes §3. Test
   `test_project_cancel_ends_started_inbox_run_project_cancelled` (also checks the late
   submit is refused).
6. **Untested paths.**
   - `TestKillFailed.test_kill_failed_retry_then_dead` drives the real supervisor
     `_advance`/`_finalize_termination` on real rows: meshd is stopped (the run keeps
     running, I8), signals are made ineffective and the clock is faked. Asserts one
     `run.kill_failed` with outbox messages to user + origin, lease kept, task still
     `running`, `kill_failed_runs()` lists it, SIGKILL re-sent after 30 s, then after the
     processes really die: `failed(kill_failed_then_dead)` with `original_reason`, still
     one event, no longer listed.
   - Idle timeout: `TestTerminationReason.test_idle_timeout_only_for_stream_liveness`
     (unit, `_adapter_liveness` patched to `stream`/`none`). An end-to-end idle-timeout
     test waits for a stream adapter (M3 smoke).
   - `assert_h5` moved to `tests/helpers.py` and is called in D11 and the three H4 tests
     (one of them while the retried run holds its lease).
   - G27 asserts 6 `auth.credential_in_run` events and exactly 1 user
     `security.credential_in_run` message for the run.
7. **A1.** Durations and suite-tripwire paths are keyed by repo root (sha256 / cksum), so
   concurrent worktree suites no longer share them. Speedup 2 or serial-phase cuts are
   planned for run 5, before J6; the gate run will be measured on an idle host.
8. **Cosmetics.** Run 3 notes: `run.kill_failed` goes to user + origin agent; §3 now cites
   P5/P2/§13. `leftovers_reported` is a dataclass field. `DEFAULT_POST_SUBMIT_GRACE_S`
   moved up next to `DEFAULT_KILL_GRACE_S`. The C15 test's real name is
   `test_c15_python_isolation_flags`; the gate report will cite that.

## 10. Review m1c-run4 fixes (folded into run 5, one commit)

1. **MAJOR, I5/J5.** I5 checks the `task.submitted` row only through the DB before the kill
   (one outbox row for lead1, `pending`); the first `inbox list [@lead1]` happens after the
   restart and returns that id once; after the second restart the row is `delivered` and
   still listed once. J5 takes the "before" set from plain's `inbox` outbox rows (all
   `pending`, including `project.completed`); the rows are unchanged after kill -9 + restart,
   and the first `inbox list [@plain]` returns exactly that id set; `project.completed` is
   asserted unconditionally.
2. **Zombie wrapper.** `_watch_one` scans leftovers before `_reap_wrapper`, and the
   termination begins (its scan fills `t.seen`) before the reap. `_wrapper_identity_alive`
   accepts meshd's own unreaped child (state `Z`, same starttime, `ppid == getpid()`). The
   group is also trusted while a seen member with its recorded starttime is in session
   `pgid` (`_session_anchored`; a sid cannot be recycled while it has a member). Unit test
   `TestZombieWrapperGroup` (an `env={}` member of a zombie leader is a target; after the reap
   only a seen member keeps it trusted). Residual: adopted wrappers are init's children, so
   after such a wrapper exits, an env-stripped member reparented away from any seen process
   is not found by the pgid number alone.
3. **Recovery branches.** `TestRecoveryBranches`: exit while meshd is down (closed from
   `exit.json`, auto-submit, no `run.adopted`, `runs_exited 1`); no `wrapper.json` with a live
   carrier (terminated, then `spawn_failed(lost_before_spawn)` with `ended_at` after the
   carrier's exit, no respawn); `_step_cancelling` (a `cancelling` task with no run →
   `cancelled`, `recovered:true`, idempotent); stale `wrapper.json` identity (a live pid with
   another starttime as `wrapper.json` pid and row `wrapper_pid`/`pgid` → `lost_on_restart`,
   never signalled). These are recovery-tier tests on a real meshd rather than unit tests
   with a patched `os.kill`: the "never signalled" outcome is checked by the process's own
   SIGTERM handler. The cancelling and stale cases set the crash state by writing mesh.db
   while meshd is down.
4. **Hygiene.** `/tmp/mh-rw-*` dirs are removed (`addCleanup`); the `__main__` guard of
   test_termination_units.py is at the end; the changed setup is listed in §9 above.
5. **A1 margin.** I4's second full replay is a 20-key spot check (every 10th key plus up to
   5 keys first created after the crash). Measured in run 5 (see its notes).
6. **Testing mode / daemon start.** meshd refuses `MESH_TESTING=1` on the default home
   (rc 2 `INVALID_ARGUMENT`, plan §17.1; unit test). `mesh daemon start` waits up to 30 s
   (`DAEMON_START_WAIT_S`) for the socket, covering boot recovery.

