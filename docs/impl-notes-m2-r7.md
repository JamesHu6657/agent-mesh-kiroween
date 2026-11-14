# M2 r7 — merge crash recovery (K14, R8)

Coder: Quest. Branch `wt/m2-r7` from 9f895c0 (r6 + r5b).

## What changed

- **mesh/daemon/merge.py**
  - `recover_merge_run(conn, run)`: §11.7 step 2 for an open `merge_test` run. It is never
    adopted. If the `wrapper.json` pid is alive with the same starttime, or any process
    carries `MESH_RUN_ID=<run>`, §11.4 termination runs right here (`_terminate_test`,
    end_reason `interrupted`, `kill.grace_s`) and is waited for. Then, in one transaction,
    the run is closed `killed` (something was terminated) or `lost` (nothing left), with
    end_reason `interrupted`, and `merge:<prj>` is released. The task stays `merging`.
    The termination is driven synchronously because meshd's termination loop skips
    `merge_test` runs (r6), and recovery runs before that loop starts anyway.
  - `decide_recovery` / `recover_merging_task`: §11.7 step 5, one transaction per task.
    - `MERGE_HEAD` exists → `merge --abort`, verify (HEAD == pre_head, clean, no
      MERGE_HEAD) → T16 `interrupted`, `details.merge_head:true`, `details.restored`.
    - HEAD ≠ pre_head, `log -1 --format=%P%n%B HEAD` has ≥ 2 parents, the second equals
      the task branch tip (`refs/heads/<branch>`) and the message contains the task id →
      reconciled T15 `merged`, `merge_commit` = HEAD.
    - Otherwise (HEAD == pre_head, or HEAD moved to something that is not our merge
      commit) → T16 `interrupted` (`head_moved`, `head` recorded when it moved).
    - A git error or a missing integration worktree → T16 `interrupted` with
      `restored:null` and the error; the task never stays in `merging`.
    - The transition reuses `_final_tx` (same merge_json/event shape as the live path),
      with `extra = {recovered: true, reconciled: <bool>}` added to merge_json and to the
      `task.merged` / `merge.failed` payload. Details are redacted.
  - `_final_tx`: the run UPDATEs are now guarded `AND status IN ('starting','running')`,
    so recovery's `killed`/`lost` close is not overwritten. The live path is unchanged
    (its run is always open there).
  - Testing-only hook `MESH_TEST_CRASH_AT=after_merge_commit` (honoured only when
    `MESH_TESTING=1`, via `supervisor._testing_mode`): SIGKILL meshd right after the merge
    commit and before T15. Kept in merge.py because supervisor.py's `CRASH_POINTS` is
    outside r7 FILES.
- **mesh/daemon/recovery.py**
  - `_step_merge_runs` right after step 2 and `_step_merging` (step 5) after step 4.
    `daemon.recovered` gets `merge_runs_killed`, `merge_runs_lost`, `merges_interrupted`,
    `merges_reconciled`, `merge_errors`.
- **mesh/daemon/handlers/project.py — outside r7 FILES, by coordinator decision
  (2026-11-09).** P5 `project cancel` blocks while a task in the project is `merging`:
  rc 6 `INVALID_TRANSITION`, `details {project_id, reason:"merge_in_progress", task_id,
  merge_run_id}`. The check comes after authz and before the approval branch, so no
  change happens at all, not even a pending approval.

## Readings and decisions

1. **Reconciled `tests`.** `"passed"` when the project has a test command, `"none"`
   when it has none (the commit happens only after the test step, §10.5). `test_log` is
   set when `merges/<mrg>/test.log` exists.
2. **Dedup.** §12.2 dedup keys include the event id, so they are new for every event.
   Re-runs are safe because every step re-reads state: a closed run is not selected
   again, and `_final_tx` skips a task that is no longer `merging` (it returns
   `left_merging`, nothing is emitted). Test (c) checks that a second recovery adds no
   event or message.
3. **Noop merges** (task branch already in integration) make no commit. A crash before
   T15 then looks like HEAD == pre_head → `interrupted`; the lead can retry (T17).
4. **pre_head missing** in merge_json (should not happen; TX1 always records it):
   MERGE_HEAD is aborted without verification (`restored:null`); a matching merge
   commit is still reconciled, with pre_head = its first parent.
5. A crash between `merge --abort` and the T16 commit is safe: the next boot finds no
   MERGE_HEAD and HEAD == pre_head → `interrupted`.

## Tests (tests/recovery/test_recovery_merge.py, new)

- `test_k14_kill9_during_test_step_interrupted` (K14 a + c, P5): test command sleeps
  20 s. While it runs (MERGE_HEAD present, MESH_RUN_ID carrier alive), `project cancel` →
  rc 6 `merge_in_progress` with the task and merge run ids, project still `active`, task
  version unchanged, no approval. `kill -9` meshd: the test process survives. Restart →
  `merge_failed`, `reason:"interrupted"`, `recovered:true`, `restored:true`; integration
  HEAD == pre_head, clean, no MERGE_HEAD; no process carries the run's MESH_RUN_ID; the
  run is `killed`/`interrupted`; lease gone; one `merge.failed`, one lead1 message.
  A second kill -9 and restart: still one event and one message, all recovery counts 0.
  doctor clean.
- `test_k14_crash_after_merge_commit_reconciled` (K14 b, I9): the `after_merge_commit`
  hook kills meshd; the task is still `merging` and integration HEAD moved. Restart →
  `merged`, `reconciled:true`, `tests:"passed"`, merge_commit = HEAD with parents
  [pre_head, task tip]; clean, no MERGE_HEAD; one `task.merged` (payload
  `reconciled:true`), no `merge.failed`; lease gone; doctor clean. A second recovery
  adds nothing.
- Both tests fail on 9f895c0 (project cancel → rc 0; no crash hook → rc 0).

## Review m2-r5b-r6 minors (folded into r7)

1. **kill_failed (minor 1).**
   - `merge.DEFAULT_KILL_GRACE_S` is now 15 (the registry default).
   - A leftovers termination that ends in `kill_failed` after a passing command is a
     failure: `error:"leftovers_kill_failed"` → T16 `tests_failed`, with no commit.
   - The run row now records §11.4: on timeout `killed`/`timeout`, and for leftovers
     kill_failed `killed`/`leftovers`. Other failures keep `exited`/`tests_failed`.
   - On kill_failed the final TX emits `run.kill_failed` with the surviving pids,
     `kind:"merge_test"` and `lease_released:true`. T16 details carry `kill_failed:true`.
   - **Recorded deviation from §11.4:** the merge test step does not keep `merge:<prj>`
     and retry. It aborts the merge (git doesn't depend on the processes), releases the
     lease and reports the survivors in the event. Keeping the lease would leave the
     task in `merging` with nobody retrying, because the termination loop skips
     `merge_test` runs and supervisor.py is outside r7.
   - Unit tests: `_final_tx` with a timeout and kill_failed result; `run_test_step` with
     `terminate.step` patched to `kill_failed` and a setsid leftover.
2. **Duplicate idempotency key (minor 2).** `store_idempotency` uses
   `INSERT OR IGNORE` and returns False when the key was already taken. The final TX
   always commits. The request returns its real outcome, the key keeps the other
   request's response, and meshd.log records the conflict. Unit test with a
   pre-inserted key: the task is `merged` and the stored row is unchanged.
3. **log_tail byte cap (minor 3).** `log_tail_capped`: last 50 lines, each line at most
   1 KiB, at most 16 KiB of UTF-8 in total, newest lines first. It cuts UTF-8 safely
   and adds `details.log_tail_truncated`. Unit test with a 300 KiB line made of
   multibyte characters.
4. **Untracked test artifacts (minor 4), recorded as a reading.** Mesh never cleans the
   integration tree, as the plan requires. For r12, usage.md should say that test
   commands must not leave untracked, non-ignored files, or later merges get rc 14
   `INTEGRATION_DIRTY`. No code change.
5. **A1 (minor 5):** see the r7 tip timing below.

## Main merged in (r8+r8b, b850295); r8 N-1; load race

- Main merged into wt/m2-r7 with no conflicts.
- **r8 N-1 (mesh/core/repos.py, outside r7 FILES, assigned by the coordinator):**
  `deliver_to_base` now runs `merge --ff-only <new sha>` in the base worktree instead of
  the branch name. It then checks that `rev-parse refs/heads/<base>` equals `new`; if
  not, it raises rc 1 `git_failed` (`step:"verify base after ff"`, with `expected` and
  `actual`).
  - New test `test_deliver_prune.test_k10_tag_shadowing_the_integration_branch`: a tag
    named `mesh/<prj>/integration` at the old main. Before the fix, git merged the tag
    (a no-op), deliver returned rc 0 and main stayed put; the test failed on the old code.
- **Load race (tests/integration/test_code_tasks.py, outside r7 FILES, assigned by the
  coordinator):** `test_fresh_branch_after_lease_expiry_keeps_old_worktree` ran both
  attempts with `lease.ttl_s=2`. On a loaded box, the second attempt's lease expired
  before its submit (rc 9 LEASE_EXPIRED). Only the first attempt has to expire, so the
  TTL is set back to 600 s right after the `failed`/`lease_expired` check, before the
  retry. The sibling test `test_fresh_worktree_prepared_by_refused_start_is_used`
  already did this. No assertion changed.

## A1 speed pass (coordinator, 2026-11-09)

Target: ≤ 540 s idle. No assertion, timeout or observation window changed.

1. **CLI startup (mesh/cli/commands/daemon.py, outside r7 FILES).**
   - Every `bin/mesh` call imported `asyncio` and `mesh.daemon.server`, because
     `commands.daemon` imported them at module level. They are now imported only inside
     `handle_daemon_run`, the one verb that needs them.
   - CPU per CLI process: about 200 ms before, 120–145 ms after (30 runs of
     `--version`, measured on the loaded box).
   - The suite starts a few thousand CLI processes, so the summed per-test time fell
     from 2035 s to 1652 s.
   - No behaviour change; `daemon run`, `start` and `stop` are covered by every
     harness.
2. **Idle-bound pool (tests/run_tier.py, tests/helpers.py `@idle_bound`).**
   - Four tests spend most of their time in a required wait and use little CPU:
     - J6: a 60 s idle window.
     - H8: still one run after 20 s.
     - G12: the 30 s lockout lapse.
     - H13: the stub child's 30 s life.
   - They now run in a small extra pool (at most 4) started beside the 4 workers, so
     their waits don't hold a worker slot. They still run alongside other tests, as
     before (all four were already in the parallel phase), and their bodies are
     unchanged.
   - The serial phase is unchanged.

Idle runs of `bash scripts/test.sh`, with no other suite running (checked every 10 s):

| | before (bdb5b96) | after (speed pass) |
|---|---|---|
| total | **605 s** | **453 s** |
| unit | 18.4 s | 15.6 s |
| integration | 540.5 s | 400.1 s |
| recovery | 45.3 s | 36.7 s |
| summed per-test time | 2035 s | 1652 s |

CPU during the parallel phase after the change: 75–87 % busy (4 cores).

Selected per-test times, before → after (s):
- **Idle-bound** (about the same, as expected; they no longer block workers):
  - j6: 71.6 → 76.6
  - lockout: 32.2 → 33.9
  - h8: 25.4 → 29.9
  - unrelated-children: 31.4 → 32.8
- **CLI-heavy:**
  - f7: 12.2 → 5.4
  - approvals g5: 14.1 → 7.8
  - workspace_error_inbox_and_launch: 11.0 → 4.9
  - e4: 9.7 → 3.8
  - k10 tag shadow: 9.1 → 5.5
- **r7 tests:**
  - test_recovery_merge (a): 6.8 → 5.8
  - test_recovery_merge (b): 7.7 → 6.5

Test (a)'s 20 s sleep is never waited out: the test step is killed as soon as its
MESH_RUN_ID carrier is seen, so shortening it would gain nothing.

## r7b: review m2-r7 minors 1–6 (wt/m2-r7b, from main 1b2b9bd)

All six findings are fixed in merge.py and recovery.py. The new tests are in
tests/unit/test_merge.py, tagged `ACC: K14` (plus K10/K5 for finding 6).

1. **kill_failed in recovery is reported.**
   - `_close_recovered_merge_run` emits `run.kill_failed` in the closing transaction with
     `pids` (the survivors), `kind:"merge_test"`, `lease_released:true`, `recovered:true`
     and `end_reason:"interrupted"`.
   - `recover_merging_task` reads the run's `result_json.termination` and copies
     `kill_failed:true` plus `kill_failed_pids` into the T16 details. On a reconciled
     merge they go to `merge_json` instead.
   - `pids` is now the survivors at the moment of failure (`terminate.run_targets(...)`,
     falling back to `seen` only if that is empty), on the live path too.
   - The run stays `killed`/`interrupted`: signals were sent, and the failure is in
     `result_json.termination` and the event.
   - Test: `RecoveryMergeRunsTest.test_kill_failed_in_recovery_is_reported`, with
     `terminate.step` patched; the survivor is 4242 and the gone pid 4243 is excluded.
2. **Authoritative pre_head.** `decide_recovery` no longer trusts the TX1 probe:
   - With MERGE_HEAD, it reads HEAD before `merge --abort`; a `--no-commit` merge does not
     move HEAD. It verifies against that value and returns it as `pre_head`.
   - When it reconciles, `pre_head = parents[0]`.
   - A differing probe is kept as `recorded_pre_head`.
   - The "otherwise" branch keeps `restored = (HEAD == recorded pre_head and clean)`.
     There we cannot tell a stale probe from a moved HEAD, so `restored:false` stays
     the conservative answer; the outcome is `interrupted` either way.
   - Tests: `test_merge_head_with_a_stale_pre_head` and
     `test_reconciles_a_matching_merge_commit_with_its_first_parent`.
3. **Non-reconcile cases** (`DecideRecoveryTest`, gitfixture repo under `/tmp/mh-*`):
   - second parent ≠ tip (branch moved);
   - message without the task id;
   - a non-merge commit naming the task;
   - HEAD == pre_head with no MERGE_HEAD (restored);
   - integration missing (`integration_missing`, in the finding 1 test).
   All give `interrupted`, plus the positive control.
4. **Concurrent terminations.**
   - `recover_merge_runs(conn, runs)` begins every termination first, then
     `_step_terminations` steps them together. Boot waits about one grace + KILL_WAIT_S
     (25 s) in total, not 25 s per run.
   - `_terminate_test` (live path) uses the same stepper for a single run.
     `recover_merge_run` stays as a one-run wrapper.
   - Test: `test_terminations_are_stepped_together` (two runs, about 0.6 s total, both
     begun before either finishes).
5. **No lease release after a step 2 error.**
   - `recover_merge_runs` returns the exception for that run; the run stays open.
   - `recover_merging_task` returns `run_open` for a task whose merge_test run is still
     `starting`/`running`. `_step_merging` counts it in `merge_errors` and leaves the task
     `merging`, still holding `merge:<prj>`, so the next boot retries.
   - Test: `test_open_run_after_step2_error_keeps_task_and_lease` runs the step 2 error
     and step 5 skip, then a clean second boot that closes the run and decides the task.
6. **Live pipeline by full ref and sha.**
   - The task branch is resolved once with `rev-parse --verify refs/heads/<branch>^{commit}`.
   - `merge-base --is-ancestor <sha> HEAD`, the no-op `merge_commit` and
     `merge --no-ff --no-commit <sha>` all use that sha.
   - Test: `PipelineTagShadowTest.test_k10_tag_named_like_the_task_branch`, with a tag at
     main named like the task branch. Before: a no-op merge of the tag. Now the merge's
     parents are [main, branch tip] and the tag is untouched.

Against the old merge.py and recovery.py, 6 of the 10 new tests fail: the fixes for
findings 1, 2, 4, 5 and 6. The four non-reconcile tests for finding 3 pass on both: they
pin down existing behaviour.
