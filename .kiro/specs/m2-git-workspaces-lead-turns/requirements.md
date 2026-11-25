# Requirements Document: M2 Git workspaces, merge, lead turns

Consolidated on 2026-11-09 from docs/requirements.md (§1, §5, §6), docs/plan.md (§10, §11.7 step 5, §12.3–§12.6, §16 M2), docs/acceptance-checklist.md (sections K and L, and A1/A3/A6/A8/B11), docs/impl-notes-m2-r*.md and docs/coordination/log.md; the milestone was originally planned in docs/plan.md §16 M2 and docs/coordination/plans/m2-plan.md (Kiro, 2026-11-08), and built by the coder rotation recorded in docs/coordination/log.md.

The checklist rows are authoritative; `[ACC: …]` names them. Under checklist §0 every M1 item must still pass at the M2 gate; B11 is superseded by section K.

## Introduction
M2 adds repo projects, per-task git worktrees, the merge pipeline (T14–T18) with a supervised test step, merge crash recovery, `project deliver`, `workspace prune/delete`, launched lead turns with the `mesh-actions` protocol, and session resume plumbing exercised with the fake adapter (plan §16 M2). Vendor adapters stay launch-disabled; everything runs on `fake` and `none` in `/tmp/mh-*` git fixtures.

## Requirements

### Requirement 1: Repo projects (plan §10.2)
**User Story:** As the user, I want to hand a git repo to a project, so that its lead and executors work on Mesh-managed branches instead of my branches.

#### Acceptance Criteria
1. WHEN a project is created with `--repo`, `--base-branch` and `--test-command` THE SYSTEM SHALL create `mesh/<prj>/integration` at the base commit with a worktree under `$H/workspaces/<prj>/integration`. [ACC: K1]
2. WHEN `--repo` is not a repository top level THE SYSTEM SHALL fail rc 2, and WHEN the integration branch already exists THE SYSTEM SHALL fail rc 6. [ACC: K1]
3. WHEN a repo project omits `--test-command` THE SYSTEM SHALL fail rc 2, and WHEN it passes `--test-command none` THE SYSTEM SHALL record `tests:"none"`. [ACC: K2]
4. THE SYSTEM SHALL give each lead a detached `repo/` worktree in its lead dir that is never any task's worktree. [ACC: K13]

### Requirement 2: Code-task worktrees and submission facts (plan §10.3–§10.4)
**User Story:** As a lead, I want each code task on its own branch with honest git facts in the deliverable, so that I can judge it before merging.

#### Acceptance Criteria
1. WHEN a code task starts THE SYSTEM SHALL run it in its own worktree on branch `mesh/<prj>/<tsk>`, and its commits SHALL appear only on that branch. [ACC: K3]
2. WHEN a code task is submitted with uncommitted files THE SYSTEM SHALL report `git.dirty:true` with `dirty_files` and SHALL NOT commit on the agent's behalf. [ACC: K4]

### Requirement 3: Merge pipeline (plan §10.5, T14–T18)
**User Story:** As a lead, I want merges that run the tests and never overwrite conflicts, so that the integration branch stays correct.

#### Acceptance Criteria
1. WHEN `task merge` runs on an accepted clean task THE SYSTEM SHALL merge with `--no-ff`, run the test command as a supervised run, commit, mark the task `merged`, record `tests:"passed"` and keep the test log under `$H/merges/`. [ACC: K5]
2. WHEN the task worktree is dirty THE SYSTEM SHALL fail rc 14 `WORKTREE_DIRTY` and leave integration unchanged. [ACC: K4]
3. WHEN the merge conflicts THE SYSTEM SHALL fail rc 14 `MERGE_CONFLICT` with `details.files`, abort, restore `pre_head`, leave no `MERGE_HEAD`, keep the earlier content, mark the task `merge_failed` and notify the lead. [ACC: K6]
4. WHEN the tests fail THE SYSTEM SHALL fail rc 14 `TESTS_FAILED` with `details.log_path` and leave integration clean and unchanged. [ACC: K7]
5. WHEN the repo has no git identity THE SYSTEM SHALL end `merge_failed` with reason `commit_failed` and the git stderr, and SHALL NOT set any identity. [ACC: K8]
6. WHEN `task merge` targets a task that is not `accepted`/`merge_failed`, or is called by a non-lead THE SYSTEM SHALL fail rc 6 `INVALID_TRANSITION` or rc 4. [ACC: K9]

### Requirement 4: Merge crash recovery (plan §11.7 step 5)
**User Story:** As the user, I want a crash during a merge to leave the integration branch whole, so that recovery never needs manual git surgery.

#### Acceptance Criteria
1. WHEN meshd is killed during a merge and restarts THE SYSTEM SHALL end the task `merge_failed(interrupted)` or reconcile it to `merged`, and SHALL never leave a half-merged integration worktree (no `MERGE_HEAD`, clean tree). [ACC: K14]

### Requirement 5: Deliver, prune, delete, no push (plan §10.6–§10.7)
**User Story:** As a lead, I want to fast-forward the base branch and clean up merged worktrees, so that the repo ends tidy without force or push.

#### Acceptance Criteria
1. WHEN `project deliver` runs and the base branch is an ancestor of integration THE SYSTEM SHALL fast-forward it, and WHEN it is not THE SYSTEM SHALL fail rc 14 `NOT_FAST_FORWARD` and leave the base unchanged. [ACC: K10]
2. THE SYSTEM SHALL never push: the remote refs of the fixture repo SHALL be unchanged after every K scenario. [ACC: K11]
3. WHEN `workspace prune` runs THE SYSTEM SHALL remove only merged or commit-less cancelled worktrees using `branch -d`; WHEN an agent runs `workspace delete` on unmerged work THE SYSTEM SHALL fail rc 7 with `category:"delete"`; nothing SHALL be deleted automatically. [ACC: K12]
4. THE SYSTEM SHALL keep git behind the `gitops` allow-list, extended only by `branch -d` for prune. [ACC: A6]

### Requirement 6: Lead turns for launch-mode leads (plan §12.4, §12.6)
**User Story:** As the user, I want a launch-mode lead woken in debounced batches, so that leads react to events without burning quota on polling.

#### Acceptance Criteria
1. WHEN several lead messages arrive within the debounce window THE SYSTEM SHALL start one lead turn for them and never two concurrent turns per project (`leadturn:<prj>` lease). [ACC: L1]
2. WHEN a lead turn always exits non-zero THE SYSTEM SHALL make exactly 3 attempts with backoff, then mark the messages `dead` and send one `lead.unreachable` to the user and the origin, and start no further turns. [ACC: L3]
3. WHEN the lead prompt would exceed `max_argv_prompt_bytes` THE SYSTEM SHALL cap it, name the omitted parts in the prompt, keep the messages that did not fit `pending`, and never fail with `PROMPT_TOO_LONG`. [ACC: L6]

### Requirement 7: Lead actions and supersede (plan §12.4, §8)
**User Story:** As a lead whose CLI cannot run shell commands, I want to act through a `mesh-actions` block, with the same authority as the CLI.

#### Acceptance Criteria
1. WHEN a lead turn ends with a `mesh-actions` block THE SYSTEM SHALL execute allowed actions as the lead (`lead.action_result`) and reject others such as `config.set`, reporting them as `lead.action_failed` in the next turn. [ACC: L2]
2. WHEN `set-lead` runs during the old lead's turn THE SYSTEM SHALL terminate that turn, and its later actions SHALL fail rc 9 `LEAD_SUPERSEDED`. [ACC: L4]

### Requirement 8: Resume plumbing (plan §12.3, §14.9)
**User Story:** As the user, I want vendor sessions resumed only when resume is verified, so that correctness never depends on an unproven feature.

#### Acceptance Criteria
1. WHEN an executor or lead run emits a session id THE SYSTEM SHALL store it, and WHEN the adapter's resume capability is verified THE SYSTEM SHALL pass it to the next rework run or lead turn; otherwise the next run SHALL start fresh with the feedback in its prompt. [ACC: L5]

### Requirement 9: M1 regression at the M2 gate (checklist §0)
**User Story:** As the coordinator, I want M2 to keep every M1 guarantee, so that the gate measures the whole system.

#### Acceptance Criteria
1. THE SYSTEM's `bash scripts/test.sh` SHALL pass in under 10 minutes with empty tripwires. [ACC: A1, A3]
2. THE SYSTEM's `doctor --check-invariants` SHALL be clean, including I9 for merged code tasks. [ACC: A8]
3. WHEN the M2 verbs exist THE SYSTEM SHALL no longer return `NOT_IMPLEMENTED_IN_M1` for them, and the B11 test SHALL read as superseded by K1/K9. [ACC: B11]
