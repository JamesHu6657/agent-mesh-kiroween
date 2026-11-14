# Implementation notes — M2 run r5 (merge pipeline without the test step)

Branch `wt/m2-r5`, from 873e538 (m2-r4). Spec: `docs/coordination/plans/m2-plan.md` "## Run r5".
ACC: K4 (merge clause), K6, K8, K9, B11 (merge clause superseded), A8 (I9 live in teardown).
Coordinator decisions applied: Q4 (busy merge lease → rc 11 `CONCURRENCY_LIMIT` `details.lease`),
Q5 (`merge_test` run, `run_dir=$MESH_HOME/merges/<mrg>/`, no process), Q12 (B11 conversion).

## What was built

- `mesh/daemon/merge.py` (new): `run_task_merge(server, ctx)`.
  1. Read-only probe of the integration HEAD (worker thread, no transaction).
  2. TX1: the guards in the r5 order — task exists (5) → authz (`task.merge` row of authz: lead or
     user, `[@plain]` → 4) → `kind=code` (2, `param:"kind"`) → `check_task_transition(status,
     "merging")` (T14 from `accepted`, T17 from `merge_failed`; else 6 `INVALID_TRANSITION`) → no
     `task:<tsk>` lease (6 `RUN_STILL_ACTIVE`) → `merge:<prj>` free (11 `CONCURRENCY_LIMIT`,
     `details.lease`, `run_id`, holding `task_id`) → `test_command` is `"none"` (else 2
     `NOT_IMPLEMENTED_IN_M1`, `milestone:"M2"`, `reason:"merge_tests"`, no state change; r6 removes
     it). Then T14/T17 → `merging`, the `merge_test` run (status `running`, adapter `mesh`, no
     pid), the `merge:<prj>` lease (holder_kind meshd, holder = boot id, fence = task fence_seq,
     expires = now + test_timeout_s + 120), `merge_json={mrg, pre_head, started_at, run_id}`,
     event `task.merging` (payload: merge_id, run_id, transition T14/T17, pre_head). COMMIT.
  3. Pipeline (`run_merge_pipeline`, git only, `asyncio.to_thread`, no transaction open):
     authoritative `pre_head` re-read under the lease → task worktree dirty (`status
     --porcelain=v1 --untracked-files=all`) → T16 `worktree_dirty` → integration dirty → T16
     `integration_dirty` → `merge-base --is-ancestor <branch> HEAD` → T15 `noop:true`
     (`merge_commit` = branch tip, which is an ancestor, so I9 holds) → `merge --no-ff --no-commit`
     (failure: `diff --name-only --diff-filter=U`, `merge --abort`, verify HEAD == pre_head + clean
     + no MERGE_HEAD → T16 `conflict`, `details.files`, `details.restored`) → `commit --no-edit -m
     "Merge mesh task <tsk>: <title>"` (failure: abort + verify → T16 `commit_failed`, stderr).
  4. Final TX: T15 `merged` (event `task.merged`) or T16 `merge_failed` (event `merge.failed`,
     fanned out to the lead by the existing outbox rule); `merge_json` completed (`merge_commit`,
     `noop`, `pre_head`, `tests:"none"`, `test_log:null`, or `reason` + `details`), run →
     `exited` (`exit_code` 0/1, `end_reason` `merged` or the T16 reason, `result_json`), lease
     deleted. COMMIT. Then rc 0 data, or rc 14 with the §13.2 code for the reason.
  Steps 3+4 run as one `asyncio.ensure_future` job awaited through `asyncio.shield`: a client
  disconnect (or a cancelled request coroutine) does not cancel the merge.
- `server.py`: `task.merge` removed from `PLANNED_COMMAND_MILESTONES`; a long-request branch
  before the generic one-transaction dispatch (like the waits). It stores the idempotency record
  on rc 0, runs the scheduler tick, wakes waiters. `--dry-run` runs the guards and TX1, rolls back
  and returns `would` (transition, event, effect `merge_pipeline`).
- `handlers/task.py`: `handle_task_merge` dispatches to `merge.run_task_merge` (lazy import, no cycle).
- `cli/commands/task.py`: `task merge` sends `task.merge` with a client timeout of
  `test_timeout_s + 120` s.
- `supervisor.watch_launch_runs` and recovery step 2 skip `kind='merge_test'` explicitly
  (risk 5). No other run selection needed a change: active-slot counts use
  `kind IN ('task','external')`, I1 is per `task:%` lease, I8 (lease ⇒ active run) holds for the
  merge lease while the run is `running`, and `refresh_run_caches` only tracks runs with a pid.

## Decisions / deviations (please review)

1. **K8 fails at `merge`, not at `commit`.** With no identity, git refuses `merge --no-ff
   --no-commit` itself ("Committer identity unknown"; it needs a committer for the reflog), before
   any MERGE_HEAD. A merge failure with **no unmerged files** and an identity message in stderr
   is therefore T16 `commit_failed` (rc 14 `GIT_COMMIT_FAILED`, stderr, `details.step:"merge"`);
   with unmerged files, or any other message, it stays `conflict`. The `commit` failure branch is
   kept for real commit-step failures but no hermetic test reaches it (a hook would be needed).
2. **Unexpected git failure** (cannot read HEAD/status, timeout, merge-base rc ∉ {0,1}): T16 with
   reason `git_failed` and rc 1 `INTERNAL_ERROR` (the m2-r3 rule), after an abort+verify when a
   MERGE_HEAD is left. `git_failed` is not in the §13.2 reason table; T17 retry works from it.
3. **pre_head**: TX1 stores the probe read before TX1; the pipeline re-reads HEAD once the lease
   is held and that value is the one in the final `merge_json`, in the conflict check, and in the
   error details.
4. **Client timeout**: `test_timeout_s` is not settable in M2 yet (schema default 1800), so the
   CLI uses 1800 + 120 s without an extra round trip. r6 should read the project's value if it
   becomes settable.
5. Idempotency is stored only for rc 0 (an rc 14 is a MeshError, which the generic path does not
   store either).
6. A daemon crash mid-pipeline leaves the task `merging` with the `merge:<prj>` lease and a
   `running` merge_test run; recovery skips them in r5 (r6/r7 add the handling, per the plan).
7. The task branch and worktree are kept after the merge (§10.7).

## Tests

- `tests/integration/test_merge.py` (new, 9 tests): clean merge (no-ff parents, message, run row,
  lease released, branch/worktree kept, re-merge rc 6, doctor clean), noop, K6 conflict (files,
  HEAD == pre_head, clean, no MERGE_HEAD, A's content kept, `merge_failed`, one `merge.failed` to
  lead1), K4 dirty worktree → rc 14 then T17 retry → merged (event order checked), integration
  dirty, K8 (no identity; stderr; integration restored; `git config --get user.name` empty in the
  repo and the integration worktree), K9 (running → 6, submitted → 6, `[@plain]` → 4, unknown → 5),
  kind=general → 2 `param:"kind"`, test command → 2 `merge_tests` with no state change, dry-run.
  Remote refs unchanged; the harness teardown runs `doctor --check-invariants` (I9 live, A8).
- B11 conversions: `test_projects.test_b11_not_implemented_milestones` (merge of an unknown task
  → rc 5, no milestone), `test_skeleton.test_not_implemented_stubs` (same, against the daemon the
  test already starts), `test_daemon.test_f7b_unknown_command_classification` (`task.merge`
  without args → rc 2 `INVALID_ARGUMENT` `param:"task_id"`, no milestone).
- Not covered: the rc 11 busy-lease path (a merge finishes in well under a second; holding the
  lease would need a DB write from the test or a hook). Its guard is one SELECT in TX1.

## r5b — fixes for review m2-r4b-r5 (REVISE)

Coder Quest, on `wt/m2-r5`. No existing assertion changed.

1. **Finding 1 (MAJOR), §13.4 idempotency.** The record is written by `merge.store_idempotency`
   inside the final TX (same BEGIN…COMMIT as T15/T16), for rc 0 and rc 14, with the exact
   envelope the client gets (`response_envelope`: the server's success envelope, or
   `MeshError.to_envelope`), passed through `_idempotency_record` (redaction). rc 1
   (`git_failed`, task left merging) and dry runs are never stored. server.py's merge branch
   no longer inserts; it hands `{principal_id, key, request_hash}` to the handler as
   `ctx.idempotency`. The response is built by `merge_response` before COMMIT.
   Test `test_merge.test_idempotent_replay_rc14_and_rc0`: rc 14 with key mk1, replay over the
   raw socket → rc 14, `idempotent_replay:true`, the stored request_id, one merge run, one
   `merge.failed` to lead1; then mk2 → rc 0, CLI replay → same merge_commit with
   `idempotent_replay:true`, one `task.merged`. (The CLI's error output does not print
   `idempotent_replay`, hence the raw socket for the rc 14 half; mesh/cli is not touched.)
2. **Finding 2, K8 classification.** `classify_merge_failure(unmerged, stderr)`: unmerged
   paths → `conflict`; none + identity → `commit_failed`; none otherwise → `git_failed`
   (rc 1), never `conflict`. Identity match is now locale-tolerant: besides the English
   markers, a stderr containing both `user.email` and `user.name` (git quotes the
   `git config` commands verbatim in every translation) is an identity failure. A C-locale
   env for the merge step was not done: it needs an `env` override in gitops.run_git, which is
   in r8's FILES. Unit tests with a German identity message and a non-English other error.
3. **Finding 3, task left `merging`.** `_final_tx` closes the run (`exited`, `end_reason`
   `task_left_merging`, `result_json` keeps the pipeline result incl. any `merge_commit`) and
   deletes the lease in the same TX, leaves the task status alone; the request returns rc 1
   `INTERNAL_ERROR` `reason:"task_left_merging"` (with `merge_commit`), logged to meshd.log.
   Unit test on a temp DB with the task moved to `cancelled`. **Question for the coordinator
   (r6/r7):** should P5 `project cancel` skip or block on a `merging` task (it already blocks
   `project complete` on `merge:<prj>`)? With r6 the window is up to `test_timeout_s`.
4. **Finding 4, probe.** No probe on `--dry-run`; `check_authz` runs before the probe and a
   denied caller gets no git (TX1 still raises rc 4 in the r5 order).
5. **Finding 5, details.** `run_merge_pipeline` applies `redact_value` to the failure
   details; every capped path list (`files`, `dirty_files`) carries `<name>_count` and
   `<name>_truncated`. Unit tests: an `sk-…` file name in a dirty worktree; 250 dirty paths.
6. **Finding 6, rc 11.** Unit test of `check_merge_request` on a temp init_db DB with a
   `merge:<prj>` lease and its `merge_test` run (authz patched): rc 11 `CONCURRENCY_LIMIT`,
   details `{lease, run_id, task_id}`, no status/version change.
7. **Finding 7, A1 (idle box, no other suite running, checked every minute).**
   - 873e538 (r4 tip; detached worktree): rc 0, total 504 s (real 8m24s); unit 202,
     integration 214, recovery 15.
   - r5b tip 9f417c0: rc 0, total 514 s (real 8m34s); unit 212, integration 226, recovery 15.
   - Earlier idle runs: 442dcb0 494 s; r4b 0e0fa98 507 s; r5 0612459 521 s; 54b7998 526 s.
   - M2's added time vs the last idle r3b figure (431 s) is about 83 s, under the ~90 s
     threshold, so no tests were folded.
