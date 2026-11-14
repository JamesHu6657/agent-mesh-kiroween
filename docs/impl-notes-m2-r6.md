# M2 r6 — implementation notes: the merge test step (§10.5 step 3)

Coder Quest, branch `wt/m2-r6`, from 54b7998 (r5 with the r4b fixes merged). ACC: K5, K7.

## What runs

- `merge.run_test_step(plan)` runs in the pipeline's worker thread, between
  `merge --no-ff --no-commit` and `commit`, only when the project's `test_command` is not
  `"none"`. The r5 stub (rc 2 `NOT_IMPLEMENTED_IN_M1` `reason:"merge_tests"`) is gone.
- It is the `merge_test` run that TX1 already creates (`run_dir = $H/merges/<mrg>/`).
  `spec.json` (no env values) holds argv = `test_command`, cwd = integration worktree,
  `combined_log: "test.log"`, `restore_pythonpath: true`. The wrapper is
  `python3 -P -s -m mesh.runwrap <run_dir>`, `start_new_session` (own process group),
  registered with `children` as kind `merge_test`.
- Env: `c.build_external_env` (meshd env minus every `MESH_*`, caller PYTHONPATH restored),
  plus `MESH_RUN_ID` = the merge run. No run token, no socket, no task or fence vars. The
  wrapper needs the install root on PYTHONPATH to start, so it gets it plus
  `_MESH_ORIG_PYTHONPATH`; runwrap hands the test command the caller's own PYTHONPATH back
  (`restore_pythonpath`). Tested: no `MESH_TOKEN`, no install root on the child's PYTHONPATH.
- runwrap `combined_log`: stderr goes to stdout and the one stream is redacted (§15.2, same
  line-by-line redaction and 50 MiB cap) into `test.log` (0600). No stdout.log/stderr.log.
- Deadline `projects.test_timeout_s`. On timeout the §11.4 termination runs from the pipeline
  thread (`terminate.begin(run, pgid = wrapper pid, wrapper pid, "timeout", kill.grace_s)`, then
  `terminate.step` every 200 ms until done or `kill_failed`, then `forget`). After a normal
  exit, any MESH_RUN_ID carrier or group member still alive is terminated the same way
  (end_reason `leftovers`, counted in `merge_json.test.leftovers`).
- exit.json is the source of truth. Pass = no timeout, `exit_code == 0`, no wrapper error.

## Outcomes

- Pass → `commit` → T15 with `tests:"passed"`, `test_log` = the log path. `merge_json.test`
  has `exit_code`, `signal`, `timeout`, `timeout_s`, `duration_s`, `wrapper{pid,starttime}`.
  The rc 0 data and the `task.merged` event carry `tests` and `test_log`.
- Fail or timeout → `merge --abort`, verify `pre_head` (same `_abort_and_verify` as the
  conflict path) → T16 `tests_failed`, rc 14 `TESTS_FAILED`, details: `log_path`, `log_tail`
  (last 50 lines), `timeout`, `timeout_s`, `exit_code`, `signal`, `restored`
  (+ `termination`/`leftovers`/`error` when present). `merge_json.tests` = `"failed"`.
- The final TX also stores the wrapper's pid/pgid/starttime on the run row.

## Supervisor

- `watch_launch_runs` already skips `merge_test` (r5). `advance_terminations` now skips them
  too, so a test-step termination never reaches `_advance` (T7/T8); the pipeline owns it.

## Decisions / deviations

1. `projects.test_timeout_s` has no CLI flag. The timeout path is tested on
   `run_test_step` directly with a 1 s limit (a `sh` sleeping plus a `setsid` escapee; both
   killed, `targets >= 2`). The integration tests use the default 1800 s.
2. Noop merges (branch already in integration) skip the test step: nothing new is merged.
   They keep `tests:"none"` as in r5 (step 2 finishes before step 3 in §10.5).
3. `tests.integration.test_merge` (r5 file, outside r6 FILES): the stub clause of
   `test_general_task_and_test_command_guards` asserted rc 2 `merge_tests`; with the stub
   removed it is converted B11-style to the positive result (rc 0, `tests:"passed"`, merged,
   one merge run, lease released). The `kind=general` clause is unchanged.
4. New test file helpers are borrowed from `TestMerge` by attribute (not inheritance), so the
   r5 tests do not run twice.
5. Crash mid-test: like r5, a daemon crash leaves the task `merging` with the lease held; the
   test process carries MESH_RUN_ID, so r7 recovery can find and terminate it. Not built here.

## After merging r5b (571e5b4)

- The r5b fixes apply to the test step unchanged: an rc 14 `tests_failed` response is stored
  for replay inside the final TX like every rc 14; its details (incl. `log_tail`) go through
  `redact_value`; a task that leaves `merging` during a long test run gets its run closed and
  the lease released (`task_left_merging`, rc 1). The rc 0 data and replay carry `tests` and
  `test_log`.
- Review m2-r4b-r5 ruling 4 (client timeout): the CLI still waits 1800 + 120 s. It only needs
  the project's `test_timeout_s` once that can differ from the default, and no command sets it
  yet (decision 1 above). Not changed in r6; mesh/cli is outside r6 FILES.
