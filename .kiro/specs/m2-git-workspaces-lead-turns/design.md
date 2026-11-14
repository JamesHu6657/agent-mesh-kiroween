# Design Document: M2 Git workspaces, merge, lead turns

Consolidated on 2026-11-09 from docs/plan.md (§10, §11.7, §12.3–§12.6), docs/coordination/plans/m2-plan.md (planner notes, Q1–Q15), docs/impl-notes-m2-r*.md, docs/coordination/log.md and the module list on main (853548f; merge state rechecked against `git log main` at b8d102c); the milestone was originally planned in docs/plan.md §16 M2 and docs/coordination/plans/m2-plan.md, and built by the coder rotation recorded in docs/coordination/log.md.

A summary with pointers. The M2 gate report `docs/m2-gate-report.md` is on main since the m2-r12 merge cb278d2. It was not used as a source here: the M2 gate Kiro execution checks found it out of date (it still says A1 FAIL; docs/coordination/reviews/m2-gate-kiro.md finding 1), and the docs fix run m2-r12d is in progress (prompt commit c9cb329).

## Overview
M2 connects Mesh to real git repositories without ever touching the user's branches except through `project deliver` (fast-forward only). Every repo project gets a Mesh-managed integration branch; code tasks work on their own branches in their own worktrees; the lead merges accepted work through a pipeline that runs the project's tests; launch-mode leads are woken in debounced turns and act through a `mesh-actions` block.

## Architecture
```
intake --repo  -> repos.validate_repo -> repos.setup_integration (branch + worktree) -> project row
T5 (code task) -> workspaces: worktree add -b mesh/<prj>/<tsk>   (fresh -a<n> branch after lease_expired)
submit         -> repos: git facts {head, branch, base_commit, dirty, dirty_files, diff_stat, commit_count}
task merge     -> merge.py: guards -> TX1 (merging, merge_test run, merge:<prj> lease)
                  -> merge --no-ff --no-commit -> test step (supervised merge_test run) -> commit -> T15 | T16
recovery       -> step 5: abort MERGE_HEAD / reconcile merge commit / interrupted
scheduler tick -> lead_turns.py: debounce -> TX (delivering msgs, lead_turn run, leadturn:<prj> lease)
                  -> spawn lead prompt -> exit 0: execute mesh-actions as the lead, ack | else backoff -> dead
```

## Components
| Component | Modules | Run(s) |
|---|---|---|
| Git fixture for tests (hermetic env, `/tmp/mh-*` guard) | `tests/gitfixture.py` | r1 |
| Invariant I9 | `mesh/core/invariants.py` | r1 |
| `mesh-actions` parser, lead prompt with size cap | `mesh/core/lead_actions.py`, `mesh/core/lead_prompt.py` | r2 |
| Repo validation, integration setup, lead `repo/` worktree, git facts | `mesh/core/repos.py` | r3, r4, r8 |
| Code-task worktrees | `mesh/core/workspaces.py`, `mesh/daemon/scheduler.py`, `mesh/daemon/handlers/task.py` | r4 |
| Merge pipeline and test step | `mesh/daemon/merge.py`, `mesh/runwrap.py`, `mesh/daemon/supervisor.py` | r5, r6 |
| Merge recovery | `mesh/daemon/recovery.py`, `mesh/daemon/merge.py` | r7 |
| Deliver, prune, delete; `branch -d` in the allow-list | `mesh/core/gitops.py`, `mesh/daemon/handlers/project.py`, workspace handler | r8 |
| Lead run tokens, lead turns, backoff | `mesh/core/authz.py`, `mesh/daemon/lead_turns.py`, `mesh/core/settings.py` | r9 |
| Lead actions, set-lead termination | `mesh/daemon/lead_turns.py`, `mesh/testing/fake_agent.py`, `mesh/core/events.py` | r10 |
| Session ids and resume | `mesh/adapters/fake.py`, `mesh/testing/fake_agent.py`, `mesh/daemon/supervisor.py`, `mesh/daemon/lead_turns.py` | r11 |

## Data model
No new tables; M2 uses columns that the M1 schema already had (m2-plan "Already in place"):
- `projects.repo_path`, `base_branch`, `integration_branch`, `base_commit`, `test_command_json` (argv JSON or `"none"`), `test_timeout_s`.
- `tasks.branch`, `base_commit`, `merge_json` (`mrg`, `pre_head`, `merge_commit`, `tests`, `noop`, `reconciled`), `last_session_id`.
- `runs.kind` `merge_test` and `lead_turn`, `runs.session_id`, `runs.lead_assignment_id`; `lead_assignments.session_id`.
- Lease keys `merge:<prj>` and `leadturn:<prj>`.

Coordinator decisions that shape the data (m2-plan):
- Q5: every merge creates a `merge_test` run row that holds `merge:<prj>`, even with tests `none`, so I8 ("no lease without an active run") holds.
- Q6: `merge_test` runs are never adopted after a crash; recovery terminates them and applies step 5. The testing-only hook `MESH_TEST_CRASH_AT=after_merge_commit` covers the reconcile case.
- Q7: `workspace delete` removes the task dir and keeps the branch; `branch -D` would need James.
- Q9: the testing-only setting `lead.wake_backoff_s` shortens the 30/120/600 s backoff in tests; it is rejected outside testing mode.
- Q10: lead actions run inside meshd as the lead run's principal (`via='lead_turn'`) before the run token is revoked, re-checking that the lead assignment is current.

## Error handling
- Merge reasons map to codes (plan §13.2): `conflict` → `MERGE_CONFLICT`, `tests_failed` → `TESTS_FAILED`, `worktree_dirty` → `WORKTREE_DIRTY`, `integration_dirty` → `INTEGRATION_DIRTY`, `commit_failed` → `GIT_COMMIT_FAILED`, all rc 14; `interrupted` is set only by recovery.
- Every failed merge step aborts, then verifies HEAD == `pre_head`, a clean tree and no `MERGE_HEAD` before reporting (plan §10.5).
- Noop detection uses `merge-base --is-ancestor`, never locale-dependent git output (m2-plan risk 7).
- A second merge on the same project while `merge:<prj>` is held → rc 11 `CONCURRENCY_LIMIT` (m2-plan Q4).
- A stale lead token after `set-lead` → rc 9 `LEAD_SUPERSEDED`, then rc 3 once revoked (checklist L4).
- `lead.action_failed` messages do not trigger a turn by themselves, so a failing action cannot loop (m2-plan Q8).

## Testing strategy
- All git runs on `tests/gitfixture.py` repos under `/tmp/mh-*` with `GIT_CONFIG_GLOBAL` pointing at an empty file and `GIT_CONFIG_NOSYSTEM=1`; tests never point git at a Mesh checkout (m2-plan common block, risk 2).
- Test files by item: K1/K2/K13 `tests/integration/test_repo_projects.py`, `tests/unit/test_repos.py`; K3/K4 `tests/integration/test_code_tasks.py`; K4/K6/K8/K9 `tests/integration/test_merge.py`, `tests/unit/test_merge.py`; K5/K7 `tests/integration/test_merge_tests.py`; K14 `tests/recovery/test_recovery_merge.py`, `tests/unit/test_merge_recovery_minors.py`; K10/K11/K12 `tests/integration/test_deliver_prune.py`; L1/L3/L6 `tests/integration/test_lead_turns.py`, `tests/unit/test_lead_prompt.py` (`test_lead_prompt_cap`); L2/L4 `tests/integration/test_lead_actions.py`, `tests/unit/test_lead_actions.py`; L5 `tests/integration/test_resume.py`; A8 (I9) `tests/unit/test_invariants.py`.
- Every K scenario asserts the fixture's remote refs are unchanged (K11).
- Suite time: loaded main suites took 834–1341 s and an idle run 514 s after r5b (log, undated "A1:" line after the 2026-11-09 10:15 entry); the gate needs an idle measurement under 600 s (A1; same line: "Remeasure idle at M2 gate"). The M2 gate Kiro execution checks measured 556 s and 555 s idle on the merged tree, 678 tests, rc 0 (docs/coordination/reviews/m2-gate-kiro.md).
