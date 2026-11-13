# M2 run r4 (m2-worktrees) — implementation notes

Branch `wt/m2-r4` from 442dcb0 (r3 + r3b merged with main: M1c complete).
Coder: Quest (author `Quest <coder@example.invalid>`, Q15). Items: K3, K4 (deliverable
clause). Coordinator decision applied: Q13.

## 1. Commits

| Sub-step | Commit | Subject |
|---|---|---|
| 0 | — | risk 3 checked, no fix needed (§2) |
| 1 | 8add1f2 | `feat(m2): code-task worktrees at T5 (ACC: K3)` |
| 2 | fb6f775 | `feat(m2): submission git info for code tasks (ACC: K4)` |
| 3 | f24b9fb | `test(m2): code task integration tests (ACC: K3, K4)` |
| 4 | (this) | `docs(m2): notes for r4` |

Commit 1 builds and imports on its own; the integration tests land in commit 3 and were
run against the final tree only.

## 2. Sub-step 0: risk 3 (gitops children)

No change. `gitops.run_git` already spawns through `children.run_registered` (M1c run 3):
Popen inside `spawn_window`, the pid added to the registry under the registry lock before
`communicate`, removed after the wait. The validate-before-spawn order (A6) is unchanged:
`validate_git_args` runs before anything is spawned. The reaper only reaps pids that are
not in the registry, and the M1c run 3 reaper tests already cover a registered child.
Shell-out from a worker thread (new in r4, §3) is safe: `run_registered` takes the same
lock from any thread, and the reaper runs on the loop thread.

## 3. What was built

### 3.1 Code-task worktrees (§10.3, §6.2 T5/T6)

`core/workspaces.py`:
- `plan_code_workspace(conn, home, task, project=None)` — DB reads only. None for a
  general task.
- `run_code_workspace_plan(plan)` — git only (no DB), so it runs in a worker thread.
  Returns `{workspace_path, branch, base_commit, action}`; raises `WorkspaceError(details)`.
- `prepare_code_workspace(conn, task, project, home="")` — both, in the calling thread
  (the spec's signature; used where the caller is already off the loop).

Rules:
- **First start** (`tasks.branch` is NULL): `rev-parse --verify refs/heads/mesh/<prj>/integration`,
  then `worktree add -b mesh/<prj>/<tsk> <ws>/<prj>/tasks/<tsk> <that sha>`; `base_commit`
  is that sha. Reading: the start point is the sha just read rather than the branch name,
  so the recorded `base_commit` is exactly where the branch forked even if the integration
  branch moves between the two calls (same commit as §10.3's `mesh/<prj>/integration`).
- **Rework / retry / reassign** (branch set, dir exists): reuse. The dir must be a worktree
  (`rev-parse --show-toplevel` equals the dir) on the recorded branch, else `workspace_error`
  (`reason: not_a_worktree | wrong_branch`).
- **After `lease_expired` without `--reuse-workspace`**: the M1c retry/reassign handlers
  already move `workspace_path` to `<tsk>.a<n>`. At T5 the branch is set and that dir does
  not exist, so: `worktree add -b mesh/<prj>/<tsk>-a<n> <ws>/.../<tsk>.a<n> <old branch>`;
  `branch` and `workspace_path` are updated in the T5 tx, `base_commit` stays the original
  integration fork point (so §10.4 counts all of the task's commits). The old worktree and
  branch are kept (§10.7). A missing dir without an `.a<n>` suffix is `workspace_error`
  `reason: workspace_missing` (nothing is re-created silently).
- **Lost T5 race**: if the first-start dir already exists but `branch` is NULL (a prepare
  whose T5 tx did not commit), it is reused after the same worktree/branch check;
  `base_commit` = `merge-base <branch> <integration>`.
- `branch` and `base_commit` are written only in the T5 transaction (launch TX A, inbox
  handler tx), together with `workspace_path`.

**Off the loop and outside the T5 tx (review m2-r3 finding 5):**
- *Launch:* `spawn_launch_runs` (runs on the loop inside the tick) never runs git. For a
  queued code task that has a slot, it plans the workspace and starts a daemon worker
  thread (`mesh-ws-<tsk>`) that runs only git. A later tick (the scheduler loop ticks at
  least every 1 s) picks up the finished result and starts the run, or records T6. Results
  are keyed by `(workspace_path, branch, fence_seq)`: a task changed in between is planned
  again, never started from a stale result. Cost: up to one tick of extra latency per
  first start of a code task.
- *Inbox:* `prepare_task_start_workspace(ctx)` (handlers/task.py) is awaited by server.py
  before `BEGIN IMMEDIATE`, with the git in `asyncio.to_thread`. It only acts when the
  handler's own checks would pass (authz, inbox delivery, project active, task queued);
  otherwise the handler reports the error as before and no git runs. Concurrency and
  model checks are not repeated there: a start refused for concurrency after a successful
  prepare leaves a ready worktree that the next start reuses.
- **Deviation from FILES:** server.py gets a 6-line hook (import + `await` of the two
  pre-steps). An async step before the request transaction cannot be added from the
  handler, and finding 5 requires it. No other server change.

**Failure (T6 `workspace_error`, Q13):** new `scheduler.fail_queued_task_t6(...)` (the T6
transaction body, extracted unchanged from `check_queued_tasks_t6`, which now calls it).
Launch: the task fails, no run row is created. Inbox: the T6 is committed in its own
transaction and the call returns rc 6 `INVALID_TRANSITION` with `details.reason:
"workspace_error"`, `failed: true`, and the git step/stderr tail. The `task.failed` event
notifies the lead (outbox), as for every T6. Failure details: `workspace`, `repo`, `step`,
`git_rc`/`timeout_s`, `stderr_tail` (2000 chars), `branch`, `message`.

**Q13 create check:** `task create --kind code` on a project without a repo is rc 2
`INVALID_ARGUMENT` `details: {param: "kind", value: "code", reason: "no_repo"}`. No
existing test created such a task.

**Prompt:** `render_executor_prompt(..., branch=None)` — the T5 branch overrides the row
value (written only in TX A). The `[code]` line text is unchanged from M1c.

**Inbox start output:** `data.branch` is added for code tasks only.

### 3.2 Submission git info (§10.4)

`core/repos.py::submission_git_info(workspace, branch, base_commit)`, read-only:
`rev-parse HEAD`; `status --porcelain=v1 --untracked-files=all` (`dirty`, `dirty_files`;
a rename lists its new path); `diff --stat <base>..HEAD`; `log --format=%H <base>..HEAD`
line count. Result: `{head, branch, base_commit, dirty, dirty_files, diff_stat,
commit_count}`. Reading: `--untracked-files=all` so an uncommitted file in a new dir is
listed by name, not as `dir/`.

Failures never block the submit: the first failing step stops collection and is stored as
`git.error = {step, reason: git_failed|git_timeout|git_unavailable|workspace_missing|
no_base_commit, git_rc|timeout_s, stderr_tail}` next to what was already collected.

Submit paths (all off the loop):
- *Fenced / run-token `task submit`:* `prepare_task_submit_git(ctx)`, awaited by server.py
  before the request transaction (git in `asyncio.to_thread`), for a running code task.
  The handler uses it only for the same task and `fence_seq`; otherwise it stores
  `git.error.reason: "not_collected"` rather than run git inside the transaction.
- *Auto-submit (T7 at exit 0):* `_watch_one` starts a per-run worker (`mesh-git-<run>`) when
  the exit will auto-submit a code task and returns; the run closes on a later tick with
  `deliverable.git`. Until then the wrapper stays unreaped, so the M1c scan-before-reap
  order (review m1c-run4 §2) is unchanged. The leftovers path auto-submits with the same
  value.
- Mesh's `git` replaces any `git` key the agent sent. No auto-commit (§10.4).
- Dry runs collect nothing.

## 4. Readings and deviations (summary)

1. First-start start point is the integration sha just read (§3.1).
2. `base_commit` is kept on the fresh `-a<n>` branch (§3.1).
3. server.py hook outside FILES, required by finding 5 (§3.1).
4. Inbox prepare repeats only authz/delivery/project/status checks (§3.1).
5. Launch first start has up to one tick of extra latency (§3.1).
6. `--untracked-files=all` for `dirty_files` (§3.2).
7. Merge refusal of a dirty tree (rest of K4) is r5.

## 5. ACC coverage

| Item | Test |
|---|---|
| K3 | `tests.integration.test_code_tasks.TestCodeTasks.test_k3_two_code_tasks_each_in_own_worktree_and_branch` |
| K3 (fresh-branch rule) | `tests.integration.test_code_tasks.TestCodeTasks.test_fresh_branch_after_lease_expiry_keeps_old_worktree` |
| K3 (rework reuse) | `tests.integration.test_code_tasks.TestCodeTasks.test_rework_reuses_worktree_and_branch` |
| K3 (T6 workspace_error, inbox rc 6 + launch) | `tests.integration.test_code_tasks.TestCodeTasks.test_workspace_error_inbox_and_launch` |
| K3 (Q13 kind check; general tasks unchanged) | `tests.integration.test_code_tasks.TestCodeTasks.test_kind_code_needs_repo_project` |
| K4 (deliverable clause) | `tests.integration.test_code_tasks.TestCodeTasks.test_k4_inbox_code_task_dirty_deliverable` |
| K4 (auto-submit path) | `tests.integration.test_code_tasks.TestCodeTasks.test_k3_two_code_tasks_each_in_own_worktree_and_branch` |
| K11 (partial: remote refs unchanged) | all of the above except the Q13 test |

## 6. Files changed

mesh/core/workspaces.py, mesh/core/repos.py, mesh/core/prompts.py,
mesh/daemon/scheduler.py, mesh/daemon/supervisor.py, mesh/daemon/handlers/task.py,
mesh/daemon/server.py (hook only, §3.1), tests/integration/test_code_tasks.py (new),
docs/impl-notes-m2-r4.md. No gitops or test_gitops change (sub-step 0). No assertion
changed or removed.

## 7. Suite

Full suite (`scripts/test.sh`) on f24b9fb: rc 0. Unit 202, integration 214 (208 + 6 new),
recovery 15. Wall 12m37s; not an idle timing: a suite in mesh-wt-m2-r2 overlapped part of
it (the box was idle at launch). Baseline at 442dcb0 before the r4 edits: rc 0, wall 24m13s,
also overlapped (m2-r2 and another run in this worktree).

Targeted runs on the final tree: tests.integration.test_code_tasks (6, OK, 110 s);
test_prompts, test_gitops, test_repo_projects, test_tasks, test_execution_launch,
test_execution_inbox (69, OK).

## r4b — fixes for review m2-r4 (REVISE)

Coder Quest, on `wt/m2-r4`. Every existing assertion is unchanged; server.py is not touched.

1. **Finding 1 (MAJOR), refused start after a fresh fork.** `run_code_workspace_plan`: when
   the task has a branch and its `<tsk>.a<n>` dir exists on `mesh/<prj>/<tsk>-a<n>`, it is the
   fresh workspace an earlier prepare forked (T5 did not commit): action `fresh`, that branch,
   the plan's `base_commit`, `previous_branch` = the DB branch. A dir on the DB branch is still
   `reused`; anything else is still `wrong_branch`. When the DB branch already is `-a<n>` (T5
   committed), it is plain reuse. Test `test_fresh_worktree_prepared_by_refused_start_is_used`
   (expiry → ibx busy → retry → start rc 11 with `-a2` forked → free the slot → start rc 0 on
   `-a2`, old worktree and branch kept). It fails on 873e538 with rc 6 `wrong_branch`.
2. **Finding 2 (MAJOR), slot held during prep.** `spawn_launch_runs` keeps per-agent and global
   reservations for the rest of the tick: a code task whose worktree is planned or in flight
   counts against `max_concurrency` and `scheduler.max_total_runs`, so later candidates cannot
   take its slot. Prep only starts for a task that has a free slot now (unchanged), so §10.1's
   lazy creation holds. The per-candidate body moved to `_spawn_one` (no logic change besides the
   `"reserved"` outcome). Test `test_code_task_in_prep_holds_its_slot` (fx1 max 1 busy; general
   p0 queued before code p5; run order busy, code, general). It fails on 873e538 (general first).
3. **Finding 3, git block bounded.** `repos.bound_git_info` (used by `submission_git_info`, so
   both the fenced submit and auto-submit get it): `redact_value` on the block; `dirty_files`
   capped at 200 with `dirty_count` and `dirty_files_truncated`; `diff_stat` capped at 16 KB
   (UTF-8 boundary) with `diff_stat_truncated`. Unit tests in `tests/unit/test_repos.py`
   (301 untracked files incl. an `sk-…` name; small tree; diff_stat cap).
4. **Finding 4, submit prepare.** `prepare_task_submit_git` runs `_apply_run_token_scope`,
   `check_authz` and the read-only `verify_task_lease_and_fence` before git; any MeshError
   skips git, and the handler raises the real error in its transaction.
5. **Finding 5, eviction.** `_WS_PREP`: finished entries of tasks that are not queued candidates
   are dropped at the end of each pass; a finished result older than 10 s is not used (the
   worktree is checked again). `_GIT_INFO`: entries of runs that are no longer active are
   dropped after each watch pass.
6. **Finding 6, concurrent prepares.** `run_code_workspace_plan` holds a per-task lock shared by
   the launch worker and the inbox prepare; in addition a failed `worktree add -b` re-checks the
   dir and reuses it if it is now a worktree on the expected branch.
7. **Finding 8, recovery count.** A code task's exit deferred for `deliverable.git` is counted as
   `exit_deferred` (`daemon.recovered.runs_exit_deferred`), not `exited`; the next watch pass
   closes it as before.
8. **Finding 7, A1.** Idle-box timings are in the report to the coordinator (r4b HEAD and the
   442dcb0 baseline, each run with no other suite on the box).
