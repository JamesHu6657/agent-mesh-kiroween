# Implementation Notes — M1c Run 2 (2a sub-steps 1–3, 2b sub-steps 4–7)

Scope: `prompts/cc-m1c-run2a.md` with the corrections in `prompts/vibe-m1c-run2a3.md`.
Implementer: Quest (as coder), finishing the uncommitted WIP left by run2a3.
Sub-steps 4–7 (watch/exit loop, read verbs, ACC tests) were not started.

## 1. Sub-step 1 — harness + gVisor probe

| Probe (`tests/unit/test_proc_probe.py`) | Result on HostA (gVisor) |
|---|---|
| `/proc/<pid>/stat`: pgrp, session, starttime | PASS |
| `/proc/<pid>/environ` of another process | PASS |
| `prctl(PR_SET_CHILD_SUBREAPER=36, 1)` | PASS (rc 0) |
| Orphaned grandchild reparents to the subreaper | PASS |

The run2a3 "blocker" was a test bug: it used 34, which is `PR_SET_THP_DISABLE`
and gives EINVAL. The same wrong constant was in M1a `mesh/daemon/server.py`, so
meshd has never actually been a subreaper. Fixed: constant 36, `use_errno=True`,
explicit argtypes, and a non-zero rc (or an exception) now writes a WARNING line
to `meshd.log` instead of being swallowed.

Harness (`tests/helpers.py`): passphrase and token files moved to a sibling
`<home>-fx` fixtures dir (so a C7 grep over `$MESH_HOME` sees only Mesh data);
`hold_launch_starts()` / `release_launch_starts()`; teardown kills leftover
processes whose `/proc/<pid>/environ` names a `MESH_RUN_ID` of a run in this home
only (no name/pattern matching, §17.1 risk 4).

## 2. Sub-step 2 — redaction (§15.2)

`mesh/core/redact.py`: exact values (env names matching the §15.2 regex, ≥ 8
chars, plus registered live tokens), longest first → `[REDACTED:<name>]`; then the
nine patterns → `[REDACTED]`. Applied at:
- run logs (runwrap, line by line);
- `meshd.log` error lines (`DaemonServer.log_error`, scheduler `_log_to_meshd`);
  the request log already records only cmd/principal/request id/rc/duration;
- event `reason` and `payload` (in `record_event`, before hashing, so the audit
  chain covers the stored, redacted values);
- error envelopes (`MeshError.to_dict`: message and details);
- deliverables (`task submit`, before the size check and storage);
- relayed approval `user_statement` (relayed rows carry no user MAC, so this does
  not affect MAC verification; direct decisions store no statement).

Reading: `task log` / `run log` output redaction belongs to the read verbs
(sub-step 5) and is not done here.

## 3. Sub-step 3 — launch spawn

### FK failure root cause and fix
`mesh/core/db.py` opens connections with `isolation_level=None` (autocommit), and
`run_scheduler_tick` runs after the request transaction has committed
(server.py ~204, ~1170). `start_queued_launch_tasks` claimed "the caller owns the
transaction", so every statement of "TX A" autocommitted on its own, and the lease
insert (`leases.run_id REFERENCES runs(id)`) ran before the run row existed →
`FOREIGN KEY constraint failed`.

Fix (`mesh/daemon/supervisor.py`):
- TX A is one `with transaction(conn)` (BEGIN IMMEDIATE): run row (`starting`,
  deadline) → lease `task:<id>` → run token credential → task `running` with
  `fence_seq+1`, `attempt+1` guarded by `WHERE status='queued' AND fence_seq=<old>`
  (rowcount must be 1) → `task.started` event. Any IntegrityError rolls it all
  back and the task stays queued.
- Effect outside any transaction: run dir, `prompt.md`, `spec.json`, spawn
  `python3 -P -s -m mesh.runwrap <run_dir>`.
- TX B (own transaction): run `running`, wrapper pid, pgid, proc_starttime,
  redacted argv.
- spawn_failed (own transaction): run `spawn_failed`, task `failed(spawn_failed)` per T8,
  run token revoked.
- prompt_too_long (own transaction), guarded by `status='queued'`.
- `spawn_launch_runs` catches and logs a failure per task, so one bad task does
  not stop the queue.
- Debug writes to `logs/debug.log` and the `traceback` dump in the scheduler were
  removed; the scheduler docstring was corrected.

### Run tokens (§7.1)
Issued in TX A with scope `{role: executor, run_id, task_id, project_id, fence}`,
`expires_at = deadline + 300 s`; `revoke_run_token(conn, run_id)` exists and is
used on spawn_failed. Revocation at normal run end belongs to the exit loop
(sub-step 4) and is not wired yet.

### Executor run-token authz (§7.2, H12)
`mesh/core/authz.py` rule 3:
- command outside heartbeat/progress/submit/fail/show/project.show →
  `CAPABILITY_MISSING` (rc 4). Previously `CREDENTIAL_NOT_ALLOWED_IN_RUN`; §7.2/H12
  require CAPABILITY_MISSING, and §7.6 reserves CREDENTIAL_NOT_ALLOWED_IN_RUN for
  a *foreign credential used inside a run* (unchanged in auth.py/server.py).
- fenced writes on any task other than `scope.task_id` → `SCOPE_MISMATCH` (rc 4).
  The handler's `_apply_run_token_scope` also checks this and fills run/fence from
  the scope; the lease fence check stays exit 9.

CLI (`mesh/cli/commands/task.py`): heartbeat/progress/submit/fail default
`--run` / `--fence` from `MESH_RUN_ID` / `MESH_FENCE` when the flags are missing
(the executor prompt says credentials are already in the environment). Without
this, `task submit <other>` from inside a run died client-side with rc 2 before
the daemon could answer SCOPE_MISMATCH. Client: `MESH_SOCKET` honoured.

### Scheduler T5
After promotion; order `priority DESC, queued_at ASC, id ASC`; bounded by the
agent's `max_concurrency` and `scheduler.max_total_runs`; at most 8 starts per tick.

### runwrap
As §11.2. Two additions: if `Popen` fails, `exit.json` is still written
(`exit_code 127`, `error`); after the child exits the stream threads get a 5 s
drain grace, so an escaped descendant holding the pipes cannot hold back
`exit.json` (leftovers are the exit loop's job, §11.3).

## 4. Readings / deviations

- **launch_hold (testing hook):** `$MESH_HOME/run/launch_hold` stops T5, honoured
  only when the daemon runs in testing mode. M1b tests that assert the queued
  state of a launch task create it in `setUp` via `hold_launch_starts()`.
- **prompt_too_long test** uses the fake adapter (argv-only strategy) instead of
  kiro/readonly: kiro is not launch-enabled before M3 (`ADAPTER_UNSUPPORTED`).
  The kiro/readonly row of the strategy table is covered in `tests/unit/test_prompts.py`.
- **queue-order test** holds T5 while both tasks are created, then releases it;
  otherwise the low-priority task starts before the high one exists.
- **M1b unit expectations changed** (equality kept, code changed per §7.2/H12):
  `tests/unit/test_events.py` lines 176, 205 and `tests/unit/test_projects.py`
  lines 354, 357, 389: `CREDENTIAL_NOT_ALLOWED_IN_RUN` → `CAPABILITY_MISSING` for
  a run token running a command outside its set.

## 5. Adapted M1b tests (same commit as sub-step 3)
`hold_launch_starts()` added to `setUp` in:
- `tests/integration/test_approvals.py`
- `tests/integration/test_execution_inbox.py`
- `tests/integration/test_scheduler.py`
- `tests/integration/test_tasks.py`
No assertion was removed or loosened.

## 6. Not done (later sub-steps)
Done in run 2b, see §8 (watch/exit loop, revocation, read verbs, ACC tests).

## 7. Review m1c-run2a fixes (commit "fix(m1c): review m1c-run2a ...")

- **Finding 1 (H4, run-token fence).** For `via=run_token` the token scope's
  `run_id`/`fence` are authoritative. An explicit `--run`/`--fence` may only
  repeat them; a differing value returns 9 `FENCE_MISMATCH` before any write.
  Test: `test_execution_launch_fences.py::test_h4_run_token_explicit_fence_must_match_scope`.
  The "scope fence 1 vs lease fence 2" case is the same code path: the
  presented run/fence (run 2) differ from scope (run 1) and are refused.
- **Finding 2 (meshd-held leases).** submit/fail/heartbeat/progress branch on
  `lease.holder_kind`. With `meshd`: submit stores the deliverable and sets the
  task `submitted` but keeps the lease and leaves the run `running`; fail is
  deferred (task stays `running`, `runs.result_json.pending_failure` holds the
  redacted reason) and the watch loop applies T8 `reported` once the run's
  processes are gone; heartbeat/progress update only `heartbeat_at`, never
  `expires_at` (only the supervisor renews a meshd lease). Because the watch
  loop renews the lease every tick, "expires_at untouched by heartbeat" is not
  observable end to end; the test checks the lease stays meshd-held.
- **Finding 3 (T5 model re-check).** The tick runs the T6 queued-task check
  again right after `promote_tasks` and before `start_queued_launch_tasks`, so a
  task promoted in the same tick is failed (`model_not_allowed` /
  `adapter_unavailable`, usual event + lead message) and never launched. This is
  the same function as the inbox/queued check rather than a second copy inside
  `start_launch_run`; there is no gap between it and TX A because the tick is
  single-threaded.
- **Finding 4.** H12 asserts (rc, error code) per command; the token file write
  is gone.
- **Finding 5.** New tests: token value absent from spec.json,
  `argv_redacted_json`, wrapper and child cmdline, meshd.log; credential
  `expires_at = deadline_at + 300 s`; spawn failure; TX A conflict. The spawn
  failure is triggered by making `$MESH_HOME/runs` a regular file so the run dir
  cannot be created inside the effect step (same `except` path as a Popen
  error), since stdlib-only tests cannot patch Popen inside meshd.
- **Finding 6.** `task fail` redacts the reason before `failure_json`,
  `result_json` and the event.
- **Finding 7 (reading, not fixed).** Run logs do not redact multi-line private
  keys yet. runwrap redacts line by line, and §15.2 pattern 8 only matches a whole
  BEGIN…END string, so the base64 body lines of a PEM key printed by a child land
  in stdout.log/stderr.log unredacted. Planned for M2 hardening (M2.x redaction
  step): a bounded BEGIN…END buffer in runwrap.
- **Finding 8.** The prompt renders `mesh task progress <task_id> --message`.
- **Finding 9.** `MESH_SOCKET` is used only when neither `--home` nor
  `MESH_HOME` chose a home (home equals the default `~/.agent-mesh`).
- **Finding 10.** Rework feedback is included only when the latest move into
  `queued` came from `rework` (T13 event), not on retries.
- **Finding 11.** Subreaper message no longer says "ERROR WARNING";
  `import json` moved below the docstring in test_redact.py.
- **Reading: `max_starts=8`.** `start_queued_launch_tasks` starts at most 8 runs
  per tick to bound tick latency; the rest start on the next tick (1 s).

## 8. Run 2 — sub-steps 4–7 (run 2b, branch wt/m1c-run2b)

### Sub-step 4: watch/exit loop (§11.3)
- `watch_launch_runs` runs inside every scheduler tick (1 s loop and after each
  request), after the expiry sweeps and before T6/promotion/T5, so a slot freed
  by an exit is reused in the same tick. Each run close is its own transaction.
- The source of truth is `runs/<run>/exit.json` (written by runwrap). A wrapper
  is alive while pid + `proc_starttime` match and it is not a zombie; the loop
  reaps only that pid (`WNOHANG`).
- Wrapper gone and no exit.json → run `lost`, task T8 `failed(lost)`.
- exit.json present → the shared finder `find_run_processes(run_id, pgid)`
  (`mesh/core/proc.py`: pgrp/session = pgid, or environ `MESH_RUN_ID=<run>`,
  zombies ignored) must return nothing before the run closes. While leftovers
  exist the run stays `running`, the meshd lease is renewed and the leftovers
  are logged once to meshd.log. Killing them is §11.4 (run 3); run 2 uses no
  `spawn_*`/`ignore_sigterm` behaviour.
- Close outcomes: task already `submitted` by CLI → `run.exited_after_submit`
  event; `pending_failure` from a run-token `task fail` → T8 `failed(reported)`;
  exit 0 → T7 auto-submit (`parse_run_output`, `submitted_by:"system"`);
  otherwise T8 `failed(exit_nonzero)` with `details.exit_code` and the last 50
  stderr lines (`stderr_tail`). `signal` is stored when the child died by one.
- Every close deletes the lease and revokes the run token (G18).
- Lease renewal: the loop extends a meshd lease each tick while the wrapper
  lives; run-token heartbeats only touch `heartbeat_at` (review finding 2).
- Fake agent: `submit_via_cli` passes `$MESH_TASK_ID` (run/fence from the
  token scope); new `try_approve` and `stale_submit`; both print `rc=` and
  `error_code=` lines, never the token.

### Sub-step 5: read verbs
- New `mesh/daemon/handlers/run.py`: `run list [--task] [--status]`,
  `run show RUN`, `run log RUN [--stream] [--tail]`, `task log TSK [--run]
  [--stream] [--tail]`, `workspace list [--project]`, `workspace show TSK`.
  Removed from `PLANNED_COMMAND_MILESTONES`; CLI stubs replaced.
- Output is redacted (`redact_value`; log lines are redacted again on the way
  out although runwrap already redacted them).
- Authz (§7.3): user and relayers read all; an agent reads its own resources
  (`run list` without `--task` is filtered to readable runs; `workspace list`
  shows lead dirs only for readable projects and task dirs for readable tasks);
  an executor run token may read only its own task (`task log`, `run show/log`,
  `workspace show`); `run list` and `workspace list` with a run token → 4
  `CAPABILITY_MISSING`.
- **Reading: log size.** Without `--tail` a log read returns the last 1000
  lines per stream with `total_lines` and `truncated`; `--tail` accepts
  1…100000. Logs can reach 50 MiB, which must not become one socket reply.
- **Reading: `run list --status`** accepts the schema values (starting,
  running, exited, killed, lost, spawn_failed); the old CLI help listed
  succeeded/failed, which are task states. Help text fixed.
- **Reading: `task log` with no run** returns `run_id:null` and empty streams
  (rc 0); `--run` of another task → 2 `INVALID_ARGUMENT`.
- `workspace show` lists up to 1000 files (relative paths, `.git` skipped).
- Task dirs stay lazy (first T5, already in run 2a). Lead dir
  `workspaces/<prj>/lead/<agent_id>/` (0700) is created at P1 (intake submit /
  project create) and at set-lead (Q6), inside the handler; a filesystem error
  does not block the state change (idempotent, recreated on the next set-lead).
- Changed test expectations (verbs now implemented, not a weakening):
  `tests/unit/test_skeleton.py::test_not_implemented_stubs` and
  `tests/integration/test_daemon.py::test_f7b_unknown_command_classification`
  now assert the M1c verbs no longer return `NOT_IMPLEMENTED_IN_M1`; the M2
  and `top` assertions are unchanged.

### Sub-step 6: ACC tests
| ID | Test |
|---|---|
| C7 | test_execution_acc_run2.py::TestRun2Secrets::test_c7_no_secret_in_home_or_outputs |
| C8 | ::TestRun2Execution::test_c8_no_token_in_argv_spec_or_run_show; test_execution_launch_fences.py::test_run_token_value_never_in_argv_spec_or_logs |
| D4 | ::test_d4_model_default_and_explicit |
| D6 | ::test_d6_queued_model_removed_from_allow_list (Q2 reading: per-tick T6) |
| D11 | ::test_d11_concurrency_limit_two (pid file under the fixtures dir) |
| D12 | ::test_d12_priority_then_fifo_on_full_agent |
| D13 | ::test_d13_global_cap_one |
| D15 + A8 | ::test_d15_lowered_limit_no_kill |
| F1, F2 | ::test_f1_happy_path_events; `doctor --check-invariants` at the end of every D/F/G/H test here |
| F4 | ::test_f4_reject_rework_prompt |
| F9 | ::test_f9_distinct_task_dirs_and_lead_dir; test_read_verbs.py |
| G6 | ::test_g6_run_token_cannot_approve |
| G18 | ::test_g18_revoked_agent_and_ended_run_tokens |
| H8 | ::TestRun2SerialTiming::test_h8_exit_nonzero_no_retry (serial) |
| H11 | ::test_h11_auto_submit_by_system |
| H12 | test_execution_launch.py::test_h12_executor_run_token_authority |
| J1 | ::test_j1_inbox_wait_wakes_on_submit (serial) |
| J9 | ::test_j9_event_tail_live_ndjson (serial) |

- J9 uses `--since <max seq>` so no event is lost before the tail subscribes
  (event tail has no wait counter); "live" = each task event arrives < 2 s
  after its `ts`.
- J1 drains lead1's inbox first and holds T5 until the wait is registered.

### Not in run 2 (run 3+)
Timeouts, cancel, kill and the reaper of §11.4 (leftovers are only logged
now), crash recovery (§11.7), lead turns.
