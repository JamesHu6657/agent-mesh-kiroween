# M2 run r3 (m2-repo) — implementation notes

Branch `wt/m2-r3` from main 2210265 (M2 r1 merged; M1c runs 4–5 not merged). STATUS
OVERRIDE applied: no edits to mesh/daemon/{supervisor,scheduler,recovery}.py,
mesh/runwrap.py, mesh/testing/**, tests/helpers.py, `project cancel`, or the M1c verbs.
tests/integration/test_daemon.py is untouched (it has no `--repo` stub probe, and the
coordinator asked to stay out of it because of the run-5 merge).

Coder: Quest (author `Quest <coder@example.invalid>`, Q15).

## 1. Commits

| Sub-step | Commit subject |
|---|---|
| carry-ins | `test(m2): hermetic I9 test, harness home as git root; gitops drops inherited repo-location env (ACC: A8, A6)` |
| 1 | `feat(m2): repo validation and integration setup (core) (ACC: K1)` |
| 2 | `feat(m2): intake and project create with --repo (ACC: K1, K2, K13)` |
| 3 | `test(m2): repo project integration tests (ACC: K1, K2, K13, K11)` |
| 4 | `docs(m2): notes for r3` |

## 2. Carry-ins from review m2-r1

1. **TestInvariantI9 hermetic.** `setUp` patches `os.environ` (`mock.patch.dict`, undone
   by `addCleanup`), drops every inherited `GIT_*` and adds the fixture's hermetic git keys.
   The I9 check runs git in-process with `os.environ`, so an outer `GIT_DIR` could have
   selected another repository.
   **Reading (product):** `gitops.run_git` now drops the repository-location variables
   `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_COMMON_DIR`, `GIT_OBJECT_DIRECTORY`,
   `GIT_ALTERNATE_OBJECT_DIRECTORIES`, `GIT_NAMESPACE`, `GIT_PREFIX` from the child env.
   Every Mesh git call selects its repo by an explicit `cwd`; a meshd started from a git
   hook (which exports `GIT_DIR`) would otherwise act on the hook's repo. It keeps
   `GIT_CEILING_DIRECTORIES`/`GIT_DISCOVERY_ACROSS_FILESYSTEM` (they only narrow discovery),
   and it does not touch identity (`GIT_AUTHOR_*`/`GIT_COMMITTER_*`) or config variables:
   Mesh never sets identity or config (§7.5) and K8 depends on that. Test:
   `tests/unit/test_gitops.py::test_run_git_ignores_inherited_repo_location_env`.
2. **GitMeshHarness home as an allowed root.** `GitFixture` keeps `allowed_roots`
   (its base dir first); `allow_root(path)` accepts only `/tmp/mh-*` paths that resolve under
   `/tmp/mh-` and are not a Mesh checkout. `GitMeshHarness.__init__` registers its home, so
   fixture git helpers can query and commit in `$H/workspaces/...` worktrees (r4+). Paths
   outside every root are still refused before any spawn. Test:
   `tests/unit/test_gitfixture.py::test_harness_home_is_an_allowed_root`.

## 3. What was built

- **mesh/core/repos.py** (new; all git through gitops, explicit `cwd`):
  `validate_repo`, `parse_test_command`, `encode_test_command`, `tests_view`,
  `setup_integration`, `add_lead_repo_worktree`, `project_repo_paths`, path helpers.
- **intake submit / project create `--repo --base-branch --test-command`**
  (handlers/project.py). Order:
  1. authz;
  2. repo argument checks: `--repo` without `--base-branch` → rc 2 `param:"base_branch"`;
     missing/invalid `--test-command` → rc 2 `param:"test_command"`; a relative repo path
     → rc 2 `param:"repo"`;
  3. `check_install_path_secure(repo_paths=[repo])` → rc 2 `INSTALL_PATH_INSECURE`, before
     any git call;
  4. `validate_repo` (read-only git): not a directory / not a repo / not the top level →
     rc 2 `param:"repo"` with `details.reason`; base branch not resolving to a commit →
     rc 2 `param:"base_branch"`;
  5. the existing title/goal/criteria/lead checks;
  6. dry run: no git writes, the rows are rolled back as for every dry run;
  7. id → inserts of the project with `repo_path` (realpath), `base_branch`,
     `integration_branch`, `base_commit`, `test_command_json` (argv JSON or `"none"`),
     criteria, deps, lead assignment and the `project.created` event → as the last
     statement (since r3b, §9), `setup_integration` (branch at the base commit, worktree at
     `<workspaces.root>/<prj>/integration`, parent dir 0700);
  8. after COMMIT the `create_lead_dir` effect also adds the lead's detached `repo/`.
- **set-lead** on a repo project: the same effect adds the new lead's `repo/` (skipped if it
  exists); the old lead's stays.
- **project show** (and the create response, and the `project.created` payload) of a repo
  project carry `repo`, `base_branch`, `integration_branch`, `base_commit`,
  `tests` (`"none"` or the argv list).
- **Install-path check with repos:** meshd boot (`setup_storage`) and `mesh doctor` pass the
  repo paths of all projects (`project_repo_paths`, [] if the table cannot be read).
- **CLI:** `intake submit`/`project create` send `--repo` as an absolute path
  (`os.path.abspath`, the daemon's cwd differs); the client-side `--repo` stubs are removed,
  as is the dead `intake.submit` branch in the server's planned-command dispatch.
- `task merge`, `project deliver`, `workspace prune/delete` still report `"M2"`.

## 4. Readings and deviations

1. **Q3 order** (corrected in r3b, §9 below). Read-only validation (incl. git `rev-parse`)
   runs right after authz, so a bad repo is rc 2 whatever else is wrong. Since r3b the
   branch and worktree are created as the **last statement** of the new-project branch,
   after every insert and the `project.created` event. So a request that fails on its own
   input (e.g. an unknown `--depends-on-project`) leaves no git state. In r3 they ran before
   the insert, and the claim here that "only an internal error could cause" a later
   rollback was wrong (review m2-r3 finding 1). If `worktree add` fails or times out after
   `branch` succeeded, the branch stays (nothing is deleted automatically, §10.7) and the
   error names it, while the rows roll back.
2. **Git failure code.** A failing `git branch`/`git worktree add` is rc 1
   `INTERNAL_ERROR` with `details.reason:"git_failed"`, `step`, `git_rc`, `stderr_tail`,
   `branch` (no specific error code exists in §13.2 for this case).
3. **Synchronous git in the handler.** The repo intake runs 4–5 short git calls inside the
   request transaction (registered children via `run_registered`, so the reaper cannot steal
   them). The r3 file list keeps server.py to the install-check call site, so there is no
   pre-transaction async phase. Intake is rare and the calls take milliseconds; the merge
   pipeline (r5) is where `asyncio.to_thread` matters.
4. **Lead `repo/` is best effort**, like the lead dir: a failure is logged in meshd.log and
   set-lead creates it again if missing. It is a post-COMMIT effect, so a dry run or a
   rollback creates nothing.
5. **`--base-branch`/`--test-command` without `--repo`** are rc 2 (`param` names the flag),
   and `--repo`/`--base-branch`/`--test-command` on a follow-up (`intake submit --project`)
   are rc 2: before r3 they were silently ignored (or stubbed).
6. **`project show` of a non-repo project** has no repo keys (unchanged output for M1
   projects).
7. **`tests` storage.** `test_command_json` holds the JSON argv or the JSON string `"none"`.
   The literal must be exactly `none`; `None`, `NONE` and the JSON text `"none"` are rc 2.
8. **K1 "existing branch rc 6"** is tested at core level (Q2):
   `test_repos.TestSetupIntegration.test_existing_branch_rc6`.
9. **Install-path wiring** is tested by calling `_prepare_repo_project` with the install root,
   a dir inside it and its parent, with gitops patched to fail if called (never via the CLI
   against the Mesh checkout).

## 5. Changed assertions (B11 superseded, Q12)

- `tests/integration/test_projects.py::test_b11_not_implemented_milestones`: the
  `intake submit --repo` clause asserted rc 2 `NOT_IMPLEMENTED_IN_M1` milestone M2. It now
  asserts the K1 bad-repo result (rc 2 `INVALID_ARGUMENT`, `param:"repo"`, the path echoed)
  and that no project was created. The repo is a missing dir under the harness home (no git
  runs). The `task merge` clause is unchanged until r5. Docstring: "ACC: B11 (superseded by
  K1/K9 in M2)".
- `tests/unit/test_skeleton.py::test_not_implemented_stubs`: the `intake submit --repo`
  probe (client-side stub before) now runs against its own started daemon and asserts rc 2
  `INVALID_ARGUMENT` `param:"repo"`. The other stub probes are unchanged.
- tests/integration/test_daemon.py: no `--repo` probe exists; untouched.

## 6. ACC coverage

| ID | Status | Tests |
|---|---|---|
| K1 | full | `tests.integration.test_repo_projects.TestRepoProjects.test_repo_intake_integration_and_lead_worktrees`, `.test_bad_repo_and_base_branch`; `tests.unit.test_repos` (`TestValidateRepo`, `TestSetupIntegration.test_existing_branch_rc6` (Q2), `TestInstallPathWiring`) |
| K2 | full | both integration tests; `tests.unit.test_repos.TestParseTestCommand`, `TestInstallPathWiring.test_repo_argument_checks_before_git` |
| K13 | partial (lead `repo/` detached at creation and set-lead; "during a lead session" is r9) | `test_repo_intake_integration_and_lead_worktrees`; `tests.unit.test_repos.TestLeadRepoWorktree` |
| K11 | partial (remote refs unchanged) | both integration tests |
| B11 | repo clause superseded | `test_projects...test_b11_not_implemented_milestones`, `test_skeleton...test_not_implemented_stubs` |
| A8 | I9 test hermetic | `tests.unit.test_invariants.TestInvariantI9` |
| A6 | gitops env reading | `tests.unit.test_gitops.TestGitops.test_run_git_ignores_inherited_repo_location_env` |

## 7. Files changed

mesh/core/repos.py (new), mesh/core/gitops.py (env scrub), mesh/daemon/handlers/project.py,
mesh/daemon/server.py (install-check call site, lead `repo/` in the lead-dir effect, dead
stub branch), mesh/cli/commands/{intake,project,doctor}.py, tests/gitfixture.py,
tests/unit/{test_repos (new), test_gitfixture, test_gitops, test_invariants, test_skeleton}.py,
tests/integration/{test_repo_projects (new), test_projects}.py, docs/impl-notes-m2-r3.md.

## 8. Suite

- **Before** (2210265, three suites from three worktrees on a 4-core box): rc 0, unit 165,
  integration 194 (1206 s), recovery 1; wall 20m19s. Not a valid A1 measurement.
- **After** (d1be4f9 code, box idle, no other suite running): rc 0; unit 177 in 15.0 s,
  integration 196 in 412 s, recovery 1; wall **7m08s**; tripwire file never created. New tests:
  +12 unit (`test_repos` 10, gitops 1, gitfixture 1), +2 integration (`test_repo_projects`,
  16 s and 21 s). An earlier run on the same tree while another worktree's suite ran: rc 0,
  10m29s.

## 9. Run r3b (review m2-r3 REVISE)

Same branch, same STATUS OVERRIDE. Changed only mesh/core/repos.py,
mesh/daemon/handlers/project.py, tests/unit/test_repos.py,
tests/integration/test_repo_projects.py and this file.

1. **Finding 1 (MAJOR): no git state for a request that fails on its input.**
   `setup_integration` moved to the end of the new-project branch of `handle_intake_submit`,
   after the projects, acceptance_criteria, project_deps and lead_assignments inserts, the
   `_lead_dir_effect` call and the `project.created` event, right before the response.
   Guard unchanged (`repo_info is not None and not ctx.dry_run`). The lead-dir effect still
   reads `repo_path` only after COMMIT. New test
   `tests.integration.test_repo_projects.TestRepoProjects.test_failed_intake_leaves_no_git_state`
   (`--depends-on-project prj_unknown0000`): rc ≠ 0, no `mesh/*` branch, only the repo's own
   worktree, no project, no `<ws>/prj_*` dir, remote refs unchanged. It fails on the r3 handler
   (a `mesh/<prj>/integration` branch is left) and passes on r3b.
   **Pre-existing, not fixed (out of scope):** an unknown `--depends-on-project` id is an
   `sqlite3.IntegrityError` and so rc 1 "Daemon unexpected error", not rc 5 `NOT_FOUND`,
   for non-repo projects too. A duplicate dep id is the same.
2. **Finding 2: git timeouts.** repos.py wraps every git call in `_git(step, ...)`, which
   maps `subprocess.TimeoutExpired` (the 30 s `run_git` limit) to rc 1 `INTERNAL_ERROR`
   `reason:"git_timeout"`, `step`, `timeout_s`, and the call's context (`repo`;
   `base_branch` in validation; `branch` in setup and `worktree` for the worktree steps).
   A timeout after the branch step therefore names the branch (Q3). Tests:
   `tests.unit.test_repos.TestSetupIntegration.test_worktree_timeout_names_branch`,
   `.test_validate_timeout_is_git_timeout`.
   **Limit:** `worktree add` checks out the whole tree and runs the repo's `post-checkout`
   hook inside the request (§4.3). A repo whose checkout takes more than 30 s cannot be
   registered (rc 1 `git_timeout`, the branch stays), and meshd is blocked for that time.
   r4 must not copy the synchronous pattern (review finding 5; noted for the r4 prompt).
3. **Finding 3: `--base-branch` must be a local branch.** After the `rev-parse --verify
   B^{commit}` check, `validate_repo` requires `show-ref --verify --quiet refs/heads/B`;
   otherwise rc 2 `param:"base_branch"`, `reason:"not_a_branch"`. So a sha, `HEAD`, a tag or
   `main~0` is refused (`project deliver` advances `refs/heads/<base>`, §10.6).
   `TestValidateRepo.test_unknown_base_branch_rc2` covers main's sha, `HEAD`, a tag and `main~0`.
4. **Finding 4: follow-up repo flags tested.** `test_bad_repo_and_base_branch` now creates a
   plain project and asserts that `intake submit --project <P> --message m --repo <R>` is rc 2
   `param:"repo"`, with no `mesh/*` branch and no repo fields on the plain project. The later
   counts in that test changed from "no project" to "only the plain project" (the plain
   project and its lead dir are new in this test, not a weakened assertion).
5. **Findings 5 and 6:** no r3b change. Finding 5 goes into the r4 prompt; finding 6
   (token file under a registered repo, §7.6 mitigation 2) is for the coordinator to
   schedule (r8 or r12).
6. **Suite (r3b, 18880fc code, box idle at start):** rc 0; unit 179 in 13.9 s, integration
   197 in 417 s, recovery 1; wall **7m11s**; tripwire file never created;
   `git branch --list 'mesh/*'` in the worktree prints nothing.
