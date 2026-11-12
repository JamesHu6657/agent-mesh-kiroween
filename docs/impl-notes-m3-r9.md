# Implementation notes: m3-r9 (refs/heads hygiene)

Branch wt/m3-r9, from main d29aa79. The only Mesh code changed is `mesh/core/repos.py`.

## Step 1: merge.py uses refs/heads/<branch> (m2-r7 finding 6)
This was already on main and needed no code change. 0eef3d5 (m2 r7b), which reached main through the r7 merges, resolves the task branch once with `rev-parse --verify refs/heads/<branch>^{commit}` (merge.py:585). Both commands the plan names use that sha:
- `merge-base --is-ancestor <sha> HEAD` (:589), formerly :551.
- `merge --no-ff --no-commit <sha>` (:595), formerly :560.

r9 adds the missing test, `TestMergeRefs.test_merge_ignores_tag_named_like_task_branch`. It puts a tag named `mesh/<prj>/<tsk>` on main, then checks the merge is a real `--no-ff` merge whose parents are `[main, branch tip]`. To confirm the test catches the bug, both commands were temporarily switched back to the bare branch name: the merge became `noop: true` on the tag, and teardown's I9 check failed. The change was reverted.

## Step 2: repos.py `base_commit` from refs/heads (m2-r3b finding 2)
`validate_repo` returned the sha of `rev-parse B^{commit}`. git resolves a tag named B before refs/heads/B, so a tag `main` made `base_commit` the tag's commit. The existence check and the `not_a_branch` check are unchanged. After them, `base_commit` is now read from `rev-parse --verify --quiet refs/heads/B^{commit}`.
- Test: `TestValidateRepoRefs.test_base_commit_ignores_tag_named_like_base`. It tags `main` at an older commit and commits once more on the branch. `base_commit` must equal `refs/heads/main`. The test also shows the bare `main^{commit}` gives the tag's commit, which is the trap. It fails on the old code.

## Files
- `mesh/core/repos.py`: the step 2 hunk only.
- `tests/integration/test_merge_refs.py` (new): reuses TestMerge's setUp and helpers by copying its non-test methods, so TestMerge's tests don't run twice.
- `docs/impl-notes-m3-r9.md` (new).

No changes to the files m2-r11 touches (supervisor.py, lead_turns.py, fake adapter, droid.py).

## Suite
- Before the fix commit: rc 0 in 683 s (unit 277, integration 254, recovery 17).
- Before the test commit: rc 0, overlapped (see real time in the report).
- Both runs started with no other suite running, but another worktree's suite (m3-r5 review / m2-r11) ran alongside for most of each, so neither is an idle time; main's idle baseline is m3-a1's call.

---

## r9b: fixes for the Linear review of m3-r9 (ACC: K4)

Branch wt/m3-r9b, from main adb3608. Review: docs/coordination/reviews/linear-m3-r9.md (1 MAJOR, 3 MINOR). Code changed: `mesh/core/repos.py`, `mesh/core/workspaces.py`.

### 1. MAJOR: deliver CAS used a different snapshot than the ancestry check
`deliver_to_base` ran `merge-base --is-ancestor refs/heads/<base> refs/heads/<integ>` and only then read `old`. If the base moved A -> C in between, `update-ref <base> B C` succeeded and rolled the base back to B.
- Fix: read `old = refs/heads/<base>^{commit}` and `new = refs/heads/<integ>^{commit}` once, run `merge-base --is-ancestor <old> <new>` on those shas, and CAS with `update-ref refs/heads/<base> <new> <old>`. When the CAS fails, `_raise_if_base_moved` re-reads the base. If it is no longer `old`, the result is rc 6 `VERSION_MISMATCH` with `reason:"base_moved"`, `expected_old`, `actual`, `new`. Nothing was written and the deliver can be retried. If the base is still `old`, the error stays rc 1 `git_failed`.
- No existing deliver error fit this case. `NOT_FAST_FORWARD` would make the lead create a merge task even when a retry would succeed. So I used the general rc 6 conflict code (`VERSION_MISMATCH` is in the CONFLICT row of the exit-code table).
- Other CAS/update sites: the only `update-ref` in repos.py/workspaces.py is this one. The worktree path (`merge --ff-only <new sha>`) cannot roll back, because ff-only never moves a branch backwards. It now also uses the pinned shas for the ancestry check, and a failed ff-only goes through the same `base_moved` check. workspaces.py has no update-ref or delete. Prune/delete (`branch -d`, `worktree remove`) live in `mesh/daemon/handlers/run.py`, which is outside this run's files. They are not compare-and-swap writes and need no ancestry snapshot.
- Tests in `tests/unit/test_repos_cas.py`. A wrapper around `repos.run_git` moves the base right after the `merge-base --is-ancestor` call returns, so the interleave is deterministic with no sleeps:
  - `test_base_advanced_between_ancestry_and_cas_is_not_rolled_back`: C diverges from B. The base stays at C, and the result is rc 6 base_moved.
  - `test_base_advanced_past_integration_is_not_rolled_back`: C is a child of B. The base stays at C.
  - `test_worktree_base_advanced_reports_base_moved`: main is checked out, and a commit lands in that worktree. main stays at C, and the result is rc 6 base_moved (on main it was rc 1 git_failed).

### 2. MINOR: validate_repo resolved the bare `<base>^{commit}` first
A tag `main` pointing at a tree or blob failed that call before the qualified one ran. The order is now: `show-ref --verify refs/heads/<base>`, then `rev-parse --verify refs/heads/<base>^{commit}`, which is the only resolution that picks the base commit. When show-ref fails, a bare `rev-parse --verify <base>` runs only to choose the error wording (`not_a_branch` for a tag, sha, HEAD or `main~0`; otherwise "does not resolve"). The existing `test_unknown_base_branch_rc2` cases are unchanged and pass.
- Test: `TestValidateRepoRefs.test_base_commit_with_non_commit_tag_named_like_base` (tag at a tree, then at a blob) in `tests/integration/test_merge_refs.py`.

### 3. MINOR: recovery merge-base used bare names
Both `merge-base` calls in the first-start recovery paths (dir exists, and lost `worktree add -b` race) now go through `_fork_point`, which uses `refs/heads/<task branch>` and `refs/heads/<integration>`.
- Test: `test_recovered_base_commit_ignores_tag_named_like_integration` (integration at B, same-name tag at A: recovered base_commit is B, was A).

### 4. MINOR: other worktree paths and same-name tags
- Lead `repo/`: `worktree add --detach <path> refs/heads/<integration>`. `lead_turns.run_lead_repo_refresh` goes through `add_lead_repo_worktree`, so it is covered too. Test: `test_lead_repo_ignores_tag_named_like_integration`.
- Fresh attempt: the start point is `refs/heads/<task branch>`. Test: `test_fresh_attempt_forks_from_branch_not_same_name_tag` (on main: `worktree add` rc 255).
- Reuse: `_existing_worktree_branch` reads `rev-parse --symbolic-full-name HEAD`, compares against the full `refs/heads/<branch>`, and returns the short name. The review suggested `git symbolic-ref HEAD`, but `symbolic-ref` is not on the gitops allow-list, and gitops.py and plan §10.5 are outside this run. `--symbolic-full-name` gives the same full ref through rev-parse; a detached HEAD gives `HEAD`, as before. Test: `test_reuse_ignores_tag_named_like_task_branch` (on main: false `wrong_branch` on `heads/mesh/...`).
- The tests in (3) and (4) live in `tests/integration/test_refs_worktrees.py` and call `add_lead_repo_worktree` / `run_code_workspace_plan` directly on GitFixture repos.

### Fail-on-main check
How I ran it: with the new tests kept, I restored the sources from main (`git show main:mesh/core/repos.py > mesh/core/repos.py`, same for workspaces.py). I ran `python3 -m unittest -v tests.unit.test_repos_cas tests.integration.test_refs_worktrees tests.integration.test_merge_refs.TestValidateRepoRefs`, then copied the fixed files back (verified with `git diff --stat`). Result: all 9 new tests fail or error on main (FAILED failures=5 errors=4). The pre-existing `test_base_commit_ignores_tag_named_like_base` passes, as expected. A regression guard that passed on main (an undisturbed update_ref deliver) was dropped: `test_deliver_prune` K10 already covers that path.

### Files
- `mesh/core/repos.py`, `mesh/core/workspaces.py`.
- `tests/unit/test_repos_cas.py` (new), `tests/integration/test_refs_worktrees.py` (new), `tests/integration/test_merge_refs.py` (+1 test).
- `docs/impl-notes-m3-r9.md` (this section).

### Suite
- Focused: test_repos_cas, test_refs_worktrees, test_merge_refs, test_repos, test_deliver_prune, test_code_tasks, test_repo_projects, test_gitops: 48 tests OK.
- Full `bash scripts/test.sh` (after the fix and test commits): rc 0, wall 771 s. Unit 349, integration 263, recovery 33, all OK. Both tripwire files were absent afterwards. `pgrep -fa run_tier.py` at the start showed another worktree's suite (`/home/node/workspace/mesh-wt-m2-r12`, integration tier), which overlapped this run, so 771 s is not an idle time. No other suite was running at the end.

---

## r9c: checked-out base movement and lead-actions flake (ACC: K4)

Branch wt/m3-r9c, from main f484e21. Follow-up to the Linear ACCEPT of r9b.

### Checked-out base advanced beyond the pinned integration commit

- After pinning A/B and passing the ancestry check, an external fast-forward of the
  checked-out base to C (a child of B) leaves `merge --ff-only B` returning success,
  "already up to date". The post-merge check now reports rc 6
  `VERSION_MISMATCH`, `reason: base_moved`, with `expected: B`, `actual: C`,
  `expected_old: A`, and `new: B`, rather than rc 1 `git_failed` with `git_rc: 0`.
- Added `TestDeliverCas.test_worktree_base_advanced_past_integration_reports_base_moved`.
  Its existing ancestry interleave hook fast-forwards the real checked-out base to C.
  It verifies both HEAD and the base remain C, integration remains B, the worktree
  stays clean, and the structured conflict names B/C.
- Fail-on-main check: ran the new test before changing product code. It failed
  (`INTERNAL_ERROR != VERSION_MISMATCH`, `git_rc: 0`, expected B / actual C).
  After the fix, the new test and all existing repo unit tests pass (19 tests).
- Existing delivery/ref callers also pass: `test_deliver_prune`, `test_merge_refs`,
  `test_refs_worktrees`, and `test_gitops` (19 tests).
- Updated `validate_repo` and `deliver_to_base` docstrings for qualified
  `refs/heads/...^{commit}` resolution and the rc 6 conflict contract.

### Lead-actions flake: asynchronous merge-start ordering

- Failing existing test:
  `TestLeadMergeAction.test_merge_action_queued_then_merged_and_other_project_refused`.
  The first 10 serial module loops and 10 parallel module-equivalent loops
  (`MESH_TEST_JOBS=4`, runner `run_single_test` in a worker pool) all passed:
  0/20 failed loops, 0/100 failed test executions, wall 738.630 s.
- Increased load to `MESH_TEST_JOBS=16`, five batches of four module-equivalent
  repetitions (each case in its own subprocess). Before the test fix:
  **1/20 failed repetitions (5%), 1/100 failed test executions**, wall 286.867 s.
  The failure was batch 3, repetition 2. Traceback excerpt (the original line 494):

  ```text
  File "tests/integration/test_lead_actions.py", line 494, in test_merge_action_queued_then_merged_and_other_project_refused
    self.assertEqual((refused["index"], refused["cmd"], refused["ok"], refused["phase"]),
  AssertionError: Tuples differ:
    (1, 'task.merge', False, 'merge_start') != (2, 'task.merge', False, 'merge_start')
  error_code: INVALID_TRANSITION
  exit_code: 6
  message: Invalid task transition from 'merging' to 'merging'
  ```

- Root cause is the test's ordering assumption, not a failed merge or shared state.
  `server.start_queued_lead_merges` schedules both queued requests as asynchronous
  tasks. `merge.run_task_merge` awaits `asyncio.to_thread(probe_integration_head, ...)`
  before TX1. Worker completion can invert TX1 order. Close-TX actions/results still
  occur in the asserted order, but either request can own the one `task.merging`
  event; the other must receive the merge-start refusal. This is the existing
  asynchronous merge-action design, not a reason to serialize the product pipeline.
- The test now derives the started request from the independent `task.merging`
  event and requires the *other* queued request's exact index/request id in the
  refusal and failure-message dedup key. It still requires exactly two queued
  requests in the same turn, all four close results in order, exactly five total
  results, exactly one merge start, the same rc 6/11 refusal, the correct lead/token
  attribution and post-close event order, successful merge, and unchanged foreign
  task. Added distinct/non-empty request-id and same-turn checks. No retries,
  sleeps, skips, timeout increases, or product changes for this flake.
- After the fix: the real CLI module passes (5 tests, wall 44.997 s). Repeated the
  same 20 module-equivalent stress repetitions at `MESH_TEST_JOBS=16`:
  **0/20 failed repetitions (0%), 0/100 failed test executions**, wall 230.491 s.
  The after-run used a read-only teardown observer; its winner count was empty
  because `task.merging.run_id` is not the lead-turn run id, so it provides no
  evidence about which worker won. Pass/fail counts come from subprocess exits.

### Final validation and files

- Full `bash scripts/test.sh`, after the final code/test changes and before the
  final commit: **rc 0, wall 903 s**, 350 unit + 263 integration + 33 recovery
  tests (646 total), all OK.
- `pgrep -fa run_tier.py` before: `11155 python3 tests/run_tier.py tests/integration`,
  cwd `/home/node/workspace/mesh-wt-m3-r4`. After: no actual `run_tier.py` process.
  Both calls also matched their own Factory execution-supervisor command lines;
  those are not suite runners. This suite overlapped the other worktree, so its
  wall time is not an idle benchmark.
- Explicit post-suite tripwire check: `/tmp/mesh-tripwire-suite-1552959537.log`
  and `/tmp/mesh-tripwire-hit.log` both absent. `git diff --check` passed.
- Files: `mesh/core/repos.py`, `tests/unit/test_repos_cas.py`,
  `tests/integration/test_lead_actions.py`, `docs/impl-notes-m3-r9.md`.
  The CAS interleave is the only new test; delivery/ref checks and lead-actions
  tests already existed. No temporary helper scripts were added.
- Done: checked-out base mismatch classification, qualified-ref/rc 6 docstrings,
  reproduced and fixed the lead-actions ordering flake, before/after stress
  counts, full-suite validation. No outstanding items.
