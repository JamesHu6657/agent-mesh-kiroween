# M1c run 3 — implementation notes

Scope: termination (§11.3, §11.4), child registry and reaper, run-session binding end to end,
cancel / project cancel, post-submit grace. Branch `wt/m1c-run3` from e5f9358.

## 1. Commits

| Commit | Sub-step | Content |
|---|---|---|
| ca162e0 | 1 | fake agent: `spawn_grandchild` / `spawn_setsid_grandchild` (DEVNULL stdio, `seconds`), `bad_passphrase` fresh pipe per call, `steal_token` via `--token-file`; unit tests |
| 91b82e2 | 2 | termination state machine, T9/T10 via `cancelling`, project cancel, E17 clock |
| 0a55469 | 2 | ACC tests C15 E17 F8 H1 H2 H3 H5 H16 H17 A8; fake `ignore_sigterm` param |
| 06b8d89 | 3 | child registry for git/probe spawns, 5 s reaper, `MESH_TEST_STUB_CHILD_S` |
| 0609313 | 3 | H13, H18, `test_proc_identity` |
| a63cde9 | 4 | `security.credential_in_run` inbox message, event carries project/task |
| a894a89 | 4 | G25 G26 G27 end to end |
| 6b508cd | fix | review m1c-run2 minors, load races, A1 speedups 1/3/4/5 (§7) |

## 2. Termination (§11.4)

- `mesh/daemon/terminate.py`: a non-blocking state machine per run. `begin` collects the
  target set (`find_run_processes`: group, session, `MESH_RUN_ID` escapees; never meshd,
  pid 1, or a registered child of another purpose) and sends SIGTERM. `step` re-scans each
  time (new escapees join the set), SIGKILLs after `kill.grace_s`, waits 10 s, then marks
  `kill_failed` and retries SIGKILL every 30 s. Every signal is identity-checked
  (pid + starttime, `signal_if_same`), so a reused pid is never signalled.
- Driven from two places: the 1 s supervisor watch pass and a 200 ms meshd loop
  (`advance_terminations`) that only touches runs being terminated.
- Triggers for a live wrapper, in order: task `cancelling` (end_reason `cancelled`, or
  `project_cancelled` when the project is cancelled), post-submit grace expired after a
  non-system submit (`post_submit_grace`), `deadline_at` passed (`timeout`), idle timeout
  (`idle_timeout`, only when the task's `idle_timeout_s > 0` and the adapter's liveness is
  `stream`; activity file mtime).
- Release condition: the run closes only when the target set is empty. Then, in one tx:
  run → `killed(end_reason)`, lease deleted, run token revoked (`_close_run`), then the task
  transition: `cancelling` → `cancelled` (T10, dependents get `task.blocked`); `running` →
  `failed(timeout|idle_timeout)` or `failed(reported)` when a `task fail` was pending, or
  `failed(kill_failed_then_dead)` if the kill had failed earlier; `submitted` stays.
- Leftovers at exit (§11.3): wrapper gone but targets remain → `run.leftovers_terminated`
  (pids) and the same termination. With exit 0 the task is auto-submitted (T7) at once and
  keeps the lease (allowed by I1), so `task accept` gets `RUN_STILL_ACTIVE` until release;
  the run then closes `exited` with the real exit code. Non-zero exit: T8 waits for release.
- `kill_failed`: `run.kill_failed` event (fan-out to user + origin agent, events.py, plan §11.4 step 2), lease kept,
  `mesh status` lists the run under `kill_failed_runs`.

## 3. Cancel

- `task cancel` with a lease: launch mode → `cancelling` (T9); the supervisor applies T10.
  Inbox mode → the fence is invalidated at once: lease deleted, external run `killed`
  (`cancelled`), task `cancelled` in the same tx (T10 "inbox: immediately").
  Tasks without a lease keep the old direct path (T22).
- `project cancel` was broken (running tasks set straight to `cancelled` while holding
  leases; `accepted` code tasks were skipped). Now: `is_task_terminal(status, kind)`;
  leased tasks go through `cancelling` (end_reason `project_cancelled`); others →
  `cancelled` with an event each; pending approvals → `cancelled` (unchanged).
  **Reading (confirmed in review m1c-run3 #2):** plan §6.1 P5 (plan.md:380) cancels all
  non-terminal tasks unconditionally, running ones via `cancelling`, and E17 expects that.
  `--cancel-running` (listed for `cancel` in §13, plan.md:977) is therefore redundant; it
  is accepted for compatibility and recorded in the `project.cancelled` payload, and its
  help says so. Open plan question (§19): P2 (plan.md:377) gives `pause` a
  `--cancel-running` flag that §13 and the CLI do not have; either drop it from P2 or
  specify it (which state the cancelled tasks enter) and build it later. No checklist
  item covers it.
- An inbox task cancelled while started (T9 → T10 at once) ends its external run
  `killed` with end_reason `cancelled`, or `project_cancelled` when it comes from P5
  (review m1c-run3 #5). Reading: §6.3 only lists `exited`/`lost` for external runs;
  Mesh cannot stop the agent, the lease is deleted and the fence is invalid, and
  `killed(<reason>)` records that the run ended by cancellation rather than by the agent.
- `project pause` kills nothing; queued tasks show `blocked_reason project_paused` (E17).

## 4. E17 decision (no_response while blocked)

Most conservative reading: the inbox `no_response` clock does not run while the task could
not be started. In `check_queued_tasks_t6`, when `compute_queue_info` says the task is
blocked by `project_paused` or `concurrency_limit`, `offered_at` is reset to now and the
task is skipped. After resume (or a free slot) the agent gets a full `inbox.ack_timeout_s`.
Tests: `test_e17_inbox_no_response_clock_paused`,
`test_e17_inbox_no_response_clock_paused_at_concurrency_limit` (added in run 4).

Consequence (review m1c-run3 #4): from run 3 on, `tasks.offered_at` means **the start
of the current acknowledgement window**, not the time `task.offered` was sent. The send
time stays in the `task.offered` event. `no_response` failure details report the window
start as `offered_at` and also as `ack_window_start`. There is no re-offer after resume or
a freed slot; the agent sees the task in its inbox as before.

## 5. Child registry and reaper

- `mesh/daemon/children.py`: `{pid: (kind, starttime)}`; `popen_registered`,
  `run_registered` (git via `gitops.run_git`, adapter probes), and
  `create_subprocess_exec_registered` register inside a `spawn_window` (in-flight counter).
- `reap_orphans`: scans `/proc/*/stat` for `Z` with `PPid == getpid()` and a pid not in the
  registry, `waitpid(pid, WNOHANG)` each; never `waitpid(-1)`. Skips the scan while a spawn
  is in flight and aborts mid-scan if one starts. Runs every 5 s and after each termination.
- The wrapper is registered at spawn and unregistered once reaped.
- `MESH_TEST_STUB_CHILD_S` (testing mode only): one registered asyncio `sleep N`, pid in
  `run/stub.pid`, asyncio returncode in `run/stub.rc`.

## 6. Run-session binding (G25–G27)

The server already rejected other credentials inside a run before verification
(`CREDENTIAL_NOT_ALLOWED_IN_RUN`, no `auth_failures` row). Added: the
`auth.credential_in_run` event now carries the run's project and task, and fan-out writes
the inbox message as type `security.credential_in_run` (dedup `cred_in_run:<run>:<recipient>`),
so the user and project lead get exactly one per run. No token text in the event.

## 7. Review m1c-run2 minors and load races (fix commit)

| Item | Resolution |
|---|---|
| Finding 1 (T22 on a leased `submitted` task) | Resolved by the T9 route rather than the interim `RUN_STILL_ACTIVE` refusal: a task with a `task:<id>` lease always goes `cancelling` → terminated → `cancelled` (task cancel and project cancel). Test: `test_h16_cancel_during_post_submit_grace` (submit_via_cli + sleep, cancel, doctor rc 0 while submitted with the lease and after). |
| Finding 2 (lead dir in the tx) | `create_lead_dir` effect after COMMIT; `test_intake_dry_run_creates_no_lead_dir` (intake and set-lead `--dry-run`). |
| Finding 3 (untested watch outcomes) | Submit/fail fences tests now release the fake and assert `run.exited_after_submit` / `failed(reported)` with one `task.failed` to lead1, lease gone, token revoked; new `test_wrapper_killed_without_exit_json_is_lost`. `pending_failure` records the reporter (principal, actor_kind, via); the T8 event uses it as the actor. |
| Finding 4 (log tail) | `collections.deque(maxlen=tail)`. |
| Finding 5 (PEM wording) | Run 2 notes say plainly that run logs do not redact multi-line keys yet (M2 hardening). |
| Finding 6 (token in idempotency row) | Stored replay envelopes are redacted (§15.2); for `agent.token.issue` the token is replaced by `[REDACTED:shown_once]`. C7 issues a token with `--idempotency-key`, greps `$H` for it, and checks the replay does not return it. |
| Finding 7 | (a) F1 reading: request-driven transitions (`assigned`, `accepted`) carry `request_id`; system ones are null, asserted both ways. (b) H8 asserts `attempt == 1`, `max_attempts == 3`. (c) D15 asserts both runs `running` immediately before and after `doctor`. |
| Finding 8 | Supervisor docstring updated; run 2 notes title; `parse_run_output` and adapter liveness use meshd's testing mode (`MESH_TESTING`), not a hard-coded `True`. |
| Note 9 | `TestApprovalsIntegration.tearDown` removes its stubs dir. |
| Load races (coordinator) | New fake behaviour `wait_release {path, timeout_s}` keeps a run alive until the test creates the file. H12, H4, read-verbs run-token, C8, C7, G18, D6, D12, D15, concurrency/queue-order and token-in-argv tests no longer depend on a short sleep outliving the assertions (held or 120 s; teardown kills by `MESH_RUN_ID`). H4 compares the fenced fields (holder, agent, run, fence, task status/fence_seq/version), not the renewal timestamps. Run 3: E17 checks the `cancelling` route from the event trail (the fake can die inside one 200 ms step); H13 uses the checklist's 30 s stub. |

### A1 speedups

- **1 (applied):** `MeshHarness.setup_cli` runs fixture-setup CLI calls in-process (same argv, auth, envelope). Used by `create_agent` (agent create + token issue) and the F1 base's config set and intake. ACC-checked calls keep `harness.cli`.
- **3 (applied):** daemon readiness polls the socket with one in-process `daemon.status` request. `MeshClient` now closes its socket when connect fails.
- **5 (applied):** `run_tier.py` runs parallel tests longest-first from the previous run's durations (`/tmp/mesh-test-durations-<uid>-<tier>.json`, override `MESH_TEST_DURATIONS`); unknown tests go first; discovery order breaks ties.
- **2 (not applied):** a class-level template home copies a live-shaped DB, lead dirs and the fixtures dir between tests; the copy must also rewrite nothing path-dependent, which today holds only because `workspaces.root` is unset. 1 and 3 remove most of the setUp cost with no new shared state; revisit if A1 is still at risk after run 4.
- **4 (applied):** H8 moved to the parallel phase. Its only timing clause is "still one run after ≥ 20 s", which load can only lengthen.

## 8. Deviations and open points

- H17: the escapee uses `{"ignore_sigterm": true}` and `kill.grace_s = 2`, so the window in
  which the lease is held after auto-submit is long enough to observe `RUN_STILL_ACTIVE`
  deterministically. Without it the escapee dies on SIGTERM in < 200 ms.
- C15: the spec adds a 2 s sleep before `fail` so the wrapper's cmdline can be read while
  attempt 2 runs.
- After `task fail` (pending failure) the agent may keep running; nothing kills it before
  its deadline. The plan gives no grace for this case; left as is.
- `spawn_failed` deletes the lease without a target scan: Popen raised, so no child exists.
- Project cancel without `--cancel-running` still cancels running tasks (via `cancelling`); see §3.
- F7 (PEM redaction) still open from run 2.

## 9. Tests and durations

- Idle host: `scripts/test.sh` RC=0 in 7m03s (unit 149 in 11 s, integration 194 in 412 s, recovery 1).
- Loaded host (4 CPU-bound loops on 4 CPUs for the whole run): RC=0 in 12m56s (integration 756 s).
- Slowest new tests (idle): H13 31.9 s (30 s stub, parallel, mostly overlapping setUp);
  E17 inbox clock 10.0 s; C15 9.3 s; G25/G26 6.8 s; H17 6.4 s; H5 6.2 s; G27 5.8 s;
  H2 5.8 s; F8 5.7 s; E17 cancel 5.4 s; H1 5.1 s; lost 4.5 s; dry-run 4.6 s; H3 4.2 s; H16 3.7 s.
  Unit `test_termination_units` (incl. H18, 200 children): about 1.1 s. No new test is serial.
