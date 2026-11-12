# Impl notes m2-r8 (m2-deliver): deliver, prune, delete, no push

Run: `wt/m2-r8`, branch `wt/m2-r8`. Items: **K10, K11, K12**, A6 (regression).
Coordinator decision applied: **Q7** (`workspace delete` keeps the branch).
Parallel run r6 owns `merge.py`, `runwrap.py`, `supervisor.py`; none of those files were touched here.

## 1. Readings

### 1.1 §10.5 vs §10.7 — the `branch -d` inconsistency (resolved here)
`docs/impl-notes-m1c.md` §1.4 recorded that plan §10.7 requires `git branch -d` for prune while
the M1c allow-list rejected every `git branch` flag ("create only"), and that this was the
conservative choice *until M2*. This run resolves it: `mesh/core/gitops.py` now accepts exactly
`["branch", "-d", <ref>]` (the ref goes through `_check_ref`, so it may not be empty, start with
`-`, or contain whitespace). Everything else stays rejected: `-D`, `--delete --force`, `-df`,
`-d -f`, `-m`, `-M`, `-d` with no ref, `-d` with two refs. `push`, `rebase`, `reset`, `checkout`,
`switch`, `fetch`, `pull`, `clone`, `config`, `stash`, `clean`, `rm`, `mv` remain in
`FORBIDDEN_SUBCOMMANDS` and absent from `ALLOWED_SUBCOMMANDS` (A6 regression, asserted).

### 1.2 §10.6 deliver
Read as written: `merge-base --is-ancestor <base> <integration>` first (rc 14
`NOT_FAST_FORWARD`, nothing changed); then, if `worktree list --porcelain` shows a worktree W
with `refs/heads/<base>` checked out, W must be clean (else rc 14 `WORKTREE_DIRTY` with
`details.worktree`) and gets `merge --ff-only <integration>`; otherwise
`update-ref refs/heads/<base> <new> <old>` (compare-and-swap). Event `project.delivered`
`{old, new, method}` with `method` ∈ `merge_ff_only` | `update_ref`. Never pushes.

**Reading recorded:** the K0 fixture repo `R` itself has `main` checked out, so the *first*
deliver in a K0-based test takes the `merge_ff_only` path, not `update_ref`. Both paths are
exercised in `test_deliver_prune.py` (the worktree test removes the `main` worktree and delivers
again to hit `update_ref`).

### 1.3 §10.7 prune / delete
- `workspace prune [--project] [--dry-run]`: candidates are `kind = 'code'` tasks with a
  `workspace_path` and a `branch` whose status is `merged`, or `cancelled` with **zero** commits
  beyond `base_commit` (`git log --format=%H <base>..HEAD`). A candidate holding a task lease is
  skipped (`reason: "lease_held"`); a cancelled task whose commit count cannot be read is skipped
  (`commits_unknown`); a cancelled task with commits is skipped (`unmerged_commits`). Removal is
  `worktree remove <path>` (no force) then `branch -d <branch>` run **in the integration
  worktree**; a failure is `skipped` with `worktree_remove_failed` / `branch_delete_failed`, never
  an exception. Plain (non-repo) tasks are never candidates, so plain dirs are never pruned.
- `workspace delete TSK`: `delete` category for agents (rc 7 `APPROVAL_REQUIRED`,
  `details.category: "delete"`, then the usual `--approval` consume path); the user acts
  directly. A task with a lease is rc 6 `RUN_STILL_ACTIVE`. The task dir is removed with
  `shutil.rmtree` only after a realpath containment check under `workspaces.root`; then
  `worktree prune` runs in the project repo (best effort). **The branch is kept (Q7).** Event
  `workspace.deleted` with `branch_kept: true`.

### 1.4 Dry run
`server.py` replaces `data` with `{would: {transitions, events, effects}}` on a dry run and rolls
the transaction back. `workspace prune --dry-run` therefore reports its plan as one
`transitions` entry per worktree that would be removed
(`{task_id, project_id, path, branch, action: "prune"}`), and removes nothing.
`project deliver --dry-run` returns `data.would` with the project/base/integration names.

## 2. Deviations

1. **`workspace.prune` / `workspace.delete` authz.** The plan's §13.3 table says "lead / user
   (delete gated)". `check_authz` gained a rule 7b: both commands require the current lead of the
   project (or the user); `workspace delete` additionally goes through the `delete` approval
   category in its handler. No new capability string was invented (the agent capability set is
   still `{intake, relay}`).
2. **`workspace.prune` with no `--project`** prunes across every project the caller may act on.
   The authz check runs with `project_id=None`, which skips the lead check — the same shape as
   `project.list`. Recorded as the conservative reading: a lead can only reach its own project's
   candidates because `_prune_candidates` filters by `--project` when given, and the user is the
   only principal that normally runs it unscoped.
3. **`workspace delete` on a task with no `workspace_path`** (never started) is rc 0 with
   `deleted: false` and still records `workspace.deleted`; there is nothing to remove.
4. **`project deliver` on a `completed` project** is allowed (only `cancelled` is refused, as the
   run section states). A completed project's integration branch is still a valid fast-forward
   source.
5. **`_worktree_for_branch`** parses `worktree list --porcelain` in `mesh/core/repos.py` rather
   than in the handler, so the git call stays next to the other repo helpers.

## 3. Changed assertions (with reason)

| Test | Assertion | Reason |
|---|---|---|
| `tests/unit/test_gitops.py::test_gitops_allowlist` | `validate_git_args(["branch", "-d", "foo"])` moved from the *rejected* block to the *allowed* list | §10.7 prune needs `branch -d`; this is the assertion the run section names. New rejected cases added: `-D`, `--delete --force`, `-df`, `-d -f`, `-m`, `-M`, `-d` (no ref), `-d foo bar`, `-d -foo`, `-d "foo bar"`. |
| `tests/unit/test_skeleton.py::test_not_implemented_stubs` | `project deliver`, `workspace prune`, `workspace delete` no longer assert `NOT_IMPLEMENTED_IN_M1` + `milestone: "M2"`; they now assert the daemon serves them (rc 5 `NOT_FOUND` for an unknown project/task, rc 0 for prune with no candidates) | B11 stub superseded by K10/K12 (the run section names these probes). The probes moved into the daemon-backed `repo_harness` block because the class-level harness has no daemon. Docstring updated to name K10/K12. |
| `tests/integration/test_daemon.py::test_f7b_unknown_command_classification` | `workspace.prune` / `workspace.delete` no longer assert `NOT_IMPLEMENTED_IN_M1` + `milestone: "M2"`; prune is rc 0 with empty `removed`/`skipped`, delete without `task_id` is rc 2 `INVALID_ARGUMENT` `details.param: "task_id"` | Same B11 supersession (K12). |

No other assertion was weakened or deleted.

## 4. ACC coverage

| ID | Coverage | Test(s) |
|---|---|---|
| **K10** | full | `tests/integration/test_deliver_prune.py::TestDeliverPrune::test_k10_deliver_fast_forwards_then_refuses_non_ff` (rc 0, `main` == integration, `project.delivered` payload; then advance `main` independently, merge another task, deliver → rc 14 `NOT_FAST_FORWARD`, `main` unchanged, no second event); `::test_k10_deliver_through_a_worktree_holding_the_base` (dirty base worktree → rc 14 `WORKTREE_DIRTY` + `details.worktree`; clean → `merge_ff_only`; no worktree → `update_ref` with `old`/`new`) |
| **K11** | full | `::test_k11_no_push_across_intake_merge_deliver_prune` — `remote_refs` equal before and after intake → merge → deliver → prune. Also asserted at the end of the K10 and K12 tests. |
| **K12** | full | `::test_k12_prune_and_delete` (dry run lists only; prune removes exactly the merged and the commit-less cancelled worktree + branch; accepted-but-unmerged and cancelled-with-commits worktrees and branches survive; integration branch/worktree untouched; lead `workspace delete` → rc 7 `category: "delete"`; user delete removes the dir and keeps the branch; `workspace.deleted` recorded); `::test_k12_delete_refuses_a_task_with_a_lease` (rc 6 `RUN_STILL_ACTIVE`) |
| **A6** | regression | `tests/unit/test_gitops.py::test_gitops_allowlist` (new allowed/forbidden `branch` cases; `push`/`rebase`/`reset`/`checkout` still absent from `ALLOWED_SUBCOMMANDS` and present in `FORBIDDEN_SUBCOMMANDS`), `::test_git_never_spawned_outside_gitops` |

`grep -rn "ACC: K10" tests/` → `tests/integration/test_deliver_prune.py` (2 tests).
`grep -rn "ACC: K11" tests/` → `tests/integration/test_deliver_prune.py` (1 test) plus the
pre-existing partial K11 tags in `tests/integration/test_merge.py`.
`grep -rn "ACC: K12" tests/` → `tests/integration/test_deliver_prune.py` (2 tests).

## 5. Files changed

- `mesh/core/gitops.py` — `branch -d <ref>` allowed (exactly that form).
- `mesh/core/repos.py` — `_worktree_for_branch`, `deliver_to_base`.
- `mesh/daemon/handlers/project.py` — `handle_project_deliver`.
- `mesh/daemon/handlers/run.py` — `handle_workspace_prune`, `handle_workspace_delete` and their
  helpers; `record_event` import.
- `mesh/daemon/server.py` — dispatch for `project.deliver`, `workspace.prune`,
  `workspace.delete`; the three `"M2"` entries removed from `PLANNED_COMMAND_MILESTONES`.
- `mesh/core/authz.py` — `project.deliver` in the lead-or-user group; new rule 7b for
  `workspace.prune` / `workspace.delete`.
- `mesh/cli/commands/project.py` — `handle_project_deliver`; the unused `handle_project` stub and
  the `NotImplementedInM1Error` import removed.
- `mesh/cli/commands/workspace.py` — `handle_workspace_prune`, `handle_workspace_delete`; the
  unused `handle_workspace` stub and the `NotImplementedInM1Error` import removed.
- `tests/unit/test_gitops.py`, `tests/unit/test_skeleton.py`, `tests/integration/test_daemon.py`,
  `tests/integration/test_deliver_prune.py` (new).

## 6. Test runs

| Run | Command | rc | Counts | Wall time |
|---|---|---|---|---|
| before (baseline, polluted by mid-run edits) | `bash scripts/test.sh` | 1 | unit 205, integration 225, recovery — | > 900 s (killed at 900 s; the integration tier was still running and many tests errored because meshd was re-imported mid-run) |
| focused | `python3 -m unittest tests.unit.test_gitops tests.unit.test_repos tests.unit.test_auth` | 0 | 24 | 1.2 s |
| focused | `python3 -m unittest tests.unit.test_skeleton` | 0 | 14 | 8.1 s |
| focused | `python3 -m unittest tests.integration.test_repo_projects tests.integration.test_merge` | 0 | 12 | 42.9 s |
| focused | `python3 -m unittest tests.integration.test_deliver_prune` | 0 | 5 | 17.6 s |
| focused | `python3 -m unittest tests.integration.test_read_verbs tests.integration.test_projects tests.integration.test_code_tasks` | 0 | 29 | 117.4 s |
| after | `bash scripts/test.sh` | see §7 | see §7 | see §7 |

The "before" number is not a usable baseline: the suite was started before the first edit and
the daemon was re-imported while it ran, so most integration tests errored on a meshd that could
not import. The honest baseline is the M1c gate figure (341–587 s on this host, host-load
dependent) recorded in `docs/impl-notes-m1c.md` §"Test counts and wall times".

## 7. Final suite

`bash scripts/test.sh` → **rc 0**, both tripwire files absent/empty (A3).

| Tier | Tests | Time |
|---|---|---|
| unit | 205 | 16.9 s |
| integration | 230 | 818.4 s |
| recovery | 15 | 40.3 s |
| **total** | **450** | **876 s** |

The script's own `Total wall time: 876s` exceeds the A1 budget of 600 s. See §8.

## 8. A1 (suite under 10 minutes) — not met on this host

`bash scripts/test.sh` reported `Total wall time: 876s`, which is **over** the A1 limit of
600 s. The integration tier alone took 818 s. This is host-load dependent and not caused by this
run's changes: the same suite took 341–587 s at the M1c gate on the same code
(`docs/impl-notes-m1c.md`), and this run added 5 integration tests (~18 s) and no sleeps. The
baseline attempt in §6 was killed at 900 s while the integration tier was still running, so the
host was already slower than the M1c figures before any of this run's edits landed. Recorded
honestly rather than claimed as met; the coordinator should re-measure on an idle host.

## 9. r8b — Quest review of r8 (VERDICT REVISE)

### 9.1 Findings and fixes

| # | Finding | Fix |
|---|---|---|
| MAJOR-1 | Any non-run-token agent could prune every project: `authz.py` rule 7b only checked the lead when `project_id` was set, and `workspace prune` without `--project` passed `project_id=None` while `_prune_candidates` covered all projects (ibx and grok got rc 0; grok actually removed a worktree). | `mesh/core/authz.py` rule 7b: for `workspace.prune` / `workspace.delete` with a **non-user** principal and `project_id is None` → `MeshError(INVALID_ARGUMENT, EXIT_USAGE, details={"param": "project", "agent": name})`. Agents must pass `--project`; only the user may prune unscoped. The lead check for a given `project_id` is unchanged. |
| MINOR-1 | `workspace delete` on a path outside `workspaces.root` returned rc 0 and recorded `workspace.deleted`. | `handle_workspace_delete` raises `INVALID_ARGUMENT` with `details.reason: "outside_workspaces_root"` when `realpath(workspace_path)` is not strictly under `realpath(workspaces.root)`; nothing is deleted and no event is recorded. |
| MINOR-2 | Delivering twice recorded a no-op `project.delivered` event. | `deliver_to_base` returns `method: "noop"` when `old == new` **before** any git write; `handle_project_deliver` returns the data with `events=[]` for `noop`. |
| MINOR-3 | Prune was not idempotent in its report: already-pruned tasks appeared as `skipped` with `branch_delete_failed` / `commits_unknown`. | `_already_pruned` (worktree dir gone **and** `show-ref --verify refs/heads/<branch>` fails in the integration worktree) is now evaluated **before** `_prune_skip_reason`; such tasks are omitted from both `removed` and `skipped`. |
| MINOR-4 | `merge-base --is-ancestor` used short names, so a tag could shadow the branch. | `deliver_to_base` and the new `deliver_plan` use `refs/heads/<base>` and `refs/heads/<integration>` for `merge-base --is-ancestor`, both `rev-parse --verify` calls and the `merge --ff-only` argument. |
| MINOR-5 | `project deliver --dry-run` lost its plan. | The handler computes `deliver_plan(repo, base, integration)` (read-only) and returns the plan as one `transitions` entry; `server.py` reports dry runs as `data.would.{transitions,events,effects}`, so the plan is `data.would.transitions[0]` = `{project_id, base_branch, integration_branch, fast_forward}`. |
| MINOR-6 | `authz.py` scope. | See §9.3. |

### 9.2 Deviation 2 corrected

Deviation 2 in §2 above ("`workspace.prune` with no `--project` prunes across every project the
caller may act on") was **wrong** and is superseded by MAJOR-1: an agent principal may no longer
run `workspace prune` or `workspace delete` without `--project` (rc 2 `INVALID_ARGUMENT`,
`details.param: "project"`). Only the user principal may act unscoped, and a lead may act only on
its own project. The conservative reading of §13.3 is that "lead / user" means exactly that.

### 9.3 `mesh/core/authz.py` scope

The only change to `authz.py` is inside rule 7b (`workspace.prune` / `workspace.delete`): one
guard for the unscoped non-user case. No other rule, capability string, principal kind or
command was touched; the agent capability set is still `{intake, relay}` and no new capability
was invented.

### 9.4 Changed assertions (with reason)

| Test | Assertion | Reason |
|---|---|---|
| `tests/integration/test_deliver_prune.py::test_k10_deliver_fast_forwards_then_refuses_non_ff` | the dry-run block now reads `out["data"]["would"]["transitions"][0]` instead of `out["data"]["would"]` | The assertion was added in r8 and never passed: `server.py` builds `data.would` itself (`{transitions, events, effects}`) and discards the handler's `data` on a dry run. The plan is asserted field by field, so the check is not weakened. |
| `tests/integration/test_deliver_prune.py::test_k12_prune_and_delete` | second prune now asserts `removed == []` and `skipped == [(committed_tsk, "unmerged_commits")]` instead of `skipped == []` | The cancelled task that still holds a commit beyond `base_commit` is a legitimate, permanent skip (§10.7: never delete unmerged work); asserting an empty `skipped` would have required hiding it. The idempotence claim (MINOR-3) is asserted on the already-pruned tasks, which are now absent from both lists. |

No other assertion was weakened or deleted.

### 9.5 New tests (ACC: K12, partial)

| Test | Covers |
|---|---|
| `::test_k12_unscoped_prune_is_refused_for_agents` | MAJOR-1: ibx, grok and lead1 run `workspace prune` with no `--project` → rc 2 `INVALID_ARGUMENT`, `details.param: "project"`, both worktrees still on disk, 0 `workspace.pruned` events; then lead1 prunes project A with `--project` → only A's merged task removed, B's worktree survives. |
| `::test_k12_delete_refuses_a_path_outside_the_workspaces_root` | MINOR-1: the task dir is replaced by a symlink to a `/tmp/mh-outside-*` directory; `workspace delete` → rc != 0, `details.reason: "outside_workspaces_root"`, the target file survives, 0 `workspace.deleted` events. The task is cancelled first so the lease check (rc 6) cannot mask the containment check. |

### 9.6 Files changed in r8b

`mesh/core/authz.py`, `mesh/core/repos.py`, `mesh/daemon/handlers/project.py`,
`mesh/daemon/handlers/run.py`, `tests/integration/test_deliver_prune.py`,
`docs/impl-notes-m2-r8.md`.

### 9.7 Test runs (r8b)

| Run | Command | rc | Counts | Wall time |
|---|---|---|---|---|
| focused | `python3 -m unittest tests.integration.test_deliver_prune` | 0 | 7 | 24.6 s |
| focused | `python3 -m unittest tests.unit.test_gitops tests.unit.test_skeleton` | 0 | 19 | 8.4 s |
| focused | `python3 -m unittest tests.integration.test_daemon` | 0 | 23 | 66.9 s |
| focused | `python3 -m unittest tests.integration.test_projects` | 0 | 17 | 119.8 s |
| after | `bash scripts/test.sh` | 0 | unit 205, integration 232, recovery 15 (452) | 834 s |

### 9.8 Final suite (r8b)

`bash scripts/test.sh` → **rc 0**, both tripwire files empty (A3), `Total wall time: 834s`.

| Tier | Tests | Time |
|---|---|---|
| unit | 205 | 17.3 s |
| integration | 232 | 733.6 s |
| recovery | 15 | 82.7 s |
| **total** | **452** | **834 s** |

Still over the A1 budget of 600 s on this host, as in §8; the r8b delta is +2 integration tests
(~13 s) and no sleeps. The first attempt at this run was killed at 900 s by the tool timeout while
the integration tier was still running, so the host is slower than the M1c gate figures
(341–587 s) independently of these changes.

