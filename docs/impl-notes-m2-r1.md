# M2 run r1 (m2-gitfix) — K0 git fixture and invariant I9

Scope of this run: the K0 git fixture that every K test uses, and invariant I9.
No product behaviour changed except the new I9 check.

## Baseline and final suite

| Run | rc | unit | integration | recovery | wall time |
|---|---|---|---|---|---|
| baseline (HEAD 6e4871f) | 0 | 113 | 146 | 1 | 400 s |
| after sub-step 1 (K0 fixture) | 0 | 118 | 146 | 1 | 346 s |
| final (after I9) | 0 | 124 | 146 | 1 | 420 s |
| r1b (review fixes) | 0 | 129 | 146 | 1 | 800 s |

Both tripwire files were empty in every run. `test_fixture_hashes` passed (agy 1.5.3 vs
fixture 1.5.2 is the expected `stale` result; nothing was changed there).

## Files changed

- `tests/gitfixture.py` (new) — `GitFixture`, `GitMeshHarness`.
- `tests/unit/test_gitfixture.py` (new) — 8 tests for the fixture itself (5 in r1, 3 added in r1b).
- `mesh/core/invariants.py` — `check_i9_merged_code_tasks`, wired into `check_invariants`.
- `tests/unit/test_invariants.py` — new `TestInvariantI9` class (8 tests); existing tests untouched.
- `docs/impl-notes-m2-r1.md` (this file).

## Fixture API (`tests/gitfixture.py`)

`GitFixture()` creates its own base dir `/tmp/mh-<rand8>-git/` (mode 0700) and an empty
`empty.gitconfig` inside it.

- `env()` — a copy of `os.environ` with **every `GIT_*` key deleted**, plus the hermetic keys
  from `git_env_vars()`: `GIT_CONFIG_GLOBAL=<base>/empty.gitconfig`, `GIT_CONFIG_NOSYSTEM=1`,
  `GIT_TERMINAL_PROMPT=0`, `GIT_CEILING_DIRECTORIES=<base>`, `LANG`/`LC_ALL=C.UTF-8`. It never
  sets `GIT_AUTHOR_*`/`GIT_COMMITTER_*`.
- `git_env_vars()` — only those hermetic keys (no copy of `os.environ`), so a harness can add
  them without clobbering its own `PATH`/`MESH_*` values.
- `git(repo, *args, check=True, identity=False)` — runs `["git", *args]` with `cwd=repo` and
  `env()`; `identity=True` prepends `-c user.name=t -c user.email=t@example.invalid`.
  Returns a `CompletedProcess` (text, captured).
- `make_k0(name="R", identity=True)` — `git init -b main`, repo-local `user.name`/`user.email`,
  `calc.py`, `tests/test_calc.py` (passing unittest), `tests/__init__.py`, `.gitignore`
  (`__pycache__/`), `git add -A`, `git commit -m init`; then `git clone --bare <R> <base>/<name>-origin.git`
  and `git remote add origin <that path>`. With `identity=False` it unsets the identity and sets
  `user.useConfigOnly=true` (the K8 copy).
- `remote_refs(repo)` — sorted `git ls-remote origin` lines.
- `head(repo, ref="HEAD")`, `is_clean(repo)`, `has_merge_head(repo)`,
  `branches(repo, pattern="*")`, `worktrees(repo)` (parsed `worktree list --porcelain`).
  `branches()` lists names with `for-each-ref --format=%(refname:short) refs/heads/<pattern>`
  (a `*` pattern is expanded to `**` so it also matches `mesh/<prj>/<tsk>`), so a branch
  checked out in a linked worktree comes back as its plain name instead of `+ mesh/...`.
- `path(*parts)` — join onto the base dir through the same guard.
- `cleanup()` — `shutil.rmtree(base)` after re-checking the `/tmp/mh-` prefix.
- `GitMeshHarness(MeshHarness)` — takes a `GitFixture`, overrides `get_env()` to
  `super().get_env()` updated with `git_env_vars()` **only** (the raw `os.environ` is never
  re-applied, so the shims stay on `PATH` and the per-harness `MESH_TRIPWIRE_FILE` survives),
  and tears down the harness first, then the fixture.

### Guard

`_assert_under_base` runs before any process starts: the path must start with `/tmp/mh-` and
its realpath must be the base dir or inside it. `git()` additionally calls `_assert_safe_args`,
which rejects the repository-selecting options `-C`, `--git-dir`, `--git-dir=*`, `--work-tree`,
`--work-tree=*` and any argument that starts with `/` but not with `/tmp/mh-`, so a path
argument cannot redirect git at the Mesh checkout either. The module docstring states exactly
what is checked. `test_guard_refuses_paths_outside_base_before_spawn` patches
`subprocess.run`/`subprocess.Popen` to fail the test if called and asserts that
`fixture.git("/home/user/workspace/mesh-fresh-v1", "status")`, `fixture.git("/tmp/not-a-repo", ...)`,
`fixture.path("..", "escape")`, `fixture.git(repo, "-C", "<mesh checkout>", "status")`,
`--git-dir=<mesh checkout>/.git`, `--git-dir <...>`, `--work-tree=<...>`, `--work-tree <...>` and
an absolute path argument outside `/tmp/mh-` all raise `AssertionError`. The Mesh checkout is
never a git cwd or repo path anywhere in the tests.

### Hermetic env — why

Plan risk 6: this box may carry an unknown global git config and auto-detected identities.
An empty `GIT_CONFIG_GLOBAL` plus `GIT_CONFIG_NOSYSTEM=1` makes every fixture repo independent
of the box, and `GIT_TERMINAL_PROMPT=0` guarantees no credential prompt can ever block a test.
Every inherited `GIT_*` variable is dropped first (plan risk 2): an inherited `GIT_DIR` would
make git ignore the guarded `cwd` and operate on the repo it names, and inherited
`GIT_AUTHOR_*`/`GIT_COMMITTER_*` would mask K8 and reach meshd through `GitMeshHarness`.
`GIT_CEILING_DIRECTORIES=<base>` stops repo discovery from walking above the fixture base.
`GIT_AUTHOR_*`/`GIT_COMMITTER_*` are deliberately absent: commits use the repo-local identity
that `make_k0` writes (a test fixture config, not Mesh config) or explicit `-c` values.
`test_env_drops_inherited_git_vars` patches `GIT_DIR` (a fixture-owned dir), `GIT_WORK_TREE`,
`GIT_AUTHOR_NAME` and `GIT_COMMITTER_EMAIL` into `os.environ` and asserts none of them appear in
`env()`, that the only `GIT_*` keys are the four hermetic ones, and that
`make_k0(identity=False)` still refuses an identity-less commit.

### No push, bare remote built locally

The bare "remote" is created with `git clone --bare <R> <base>/<name>-origin.git` and attached
with `git remote add origin <path>`. `git ls-remote origin` therefore works with no network and
no push, and K11 can compare remote refs before/after. `test_remote_refs_without_push` asserts
the bare repo has exactly `HEAD` + `refs/heads/main` at R's main head and that R has no upstream.

### Deviation: `.gitignore` in K0

`make_k0` also writes a `.gitignore` containing `__pycache__/`. Without it, running the K0
unittest suite leaves `__pycache__/` untracked and `is_clean()` (and K6's "`git status
--porcelain` empty") would be false for a reason unrelated to the item under test. This is a
fixture detail, not a change to the checklist's K0 description.

## I9 reading

`check_i9_merged_code_tasks` selects `kind='code' AND status='merged'` tasks joined to their
project and, for each, requires:

1. `merge_json` present and parseable, with a non-empty string `merge_commit`;
2. `merge_commit` matches `^[0-9a-f]{40}([0-9a-f]{24})?$` (a full SHA-1 or SHA-256 object name);
   anything else is rejected before git is called (`merge_commit is not a sha`);
3. project `repo_path` and `integration_branch` present;
4. `repo_path` is an existing directory;
5. `git cat-file -e <commit>^{commit}` rc 0 (via `mesh.core.gitops.run_git`, `cwd=repo_path`);
6. `git merge-base --is-ancestor <commit> refs/heads/<integration_branch>`: rc 0 is clean,
   rc 1 is `merge_commit is not an ancestor of the integration branch`, any other rc is
   `git merge-base failed (rc N): <stderr tail>` (an unknown integration branch or a
   non-repo is a git error, not a real negative). `refs/heads/` avoids a tag/branch clash.

Any failure — including a missing repo dir, a git error, or an exception from `run_git` — is
exactly one `I9` violation carrying `task_id`, `project_id` and a short `reason` (plus
`merge_commit`/`repo_path`/`integration_branch` where relevant). The check returns immediately
when the query yields no rows, so no git process starts without merged code tasks
(`test_i9_no_merged_tasks_starts_no_git` patches `mesh.core.gitops.subprocess.run` to fail if
called). I1–I8 code and the violation output format are unchanged; `doctor --check-invariants`
needed no change because it already calls `check_invariants`.

## ACC coverage

| ID | Status | Tests |
|---|---|---|
| A8 (the "I9 from M2" clause) | **partial** — I9 is implemented and unit-tested; real merged tasks arrive with r5 | `tests.unit.test_invariants.TestInvariantI9` (8 tests: clean strict-ancestor merge commit, non-ancestor, bogus sha, non-sha `merge_commit`, merge-base error, NULL `merge_json`, missing repo dir, no merged tasks ⇒ no git) |

The K0 fixture has no ACC tag of its own (supporting work for K1–K14); its tests live in
`tests/unit/test_gitfixture.py` (8 tests, no `ACC:` token in the module docstring).

## Changed assertions

None. No existing assertion was weakened or removed; `tests/unit/test_invariants.py` only gained
a new class and imports. In r1b the I9 positive test was strengthened (it now asserts a real
second parent, a different sha from `main`'s initial commit, and a strict-ancestor position) and
`_merge_commit_on_integration` was rewritten to merge on the integration branch; no other
existing assertion changed.

## r1b — Kiro review fixes (review m2-r1, items 1–7)

| Item | Change | Tests |
|---|---|---|
| 1 | `git_env_vars()` added; `env()` drops every inherited `GIT_*` then adds the hermetic keys; `GitMeshHarness.get_env()` = `super().get_env()` + `git_env_vars()` only | `test_get_env_keeps_harness_env_and_adds_git_keys` |
| 2 | `branches()` uses `for-each-ref --format=%(refname:short)` (`*` → `**`) | `test_branches_returns_plain_names_for_worktree_branches` |
| 3 | `git()` rejects `-C`, `--git-dir[=]`, `--work-tree[=]` and absolute args outside `/tmp/mh-`; docstring matches | `test_guard_refuses_paths_outside_base_before_spawn` |
| 4 | `ACC: A8` token removed from the fixture test docstring; tests (a)–(d) added | `TestGitMeshHarness`, `test_env_drops_inherited_git_vars`, `test_branches_…`, `test_guard_…` |
| 5 | `_merge_commit_on_integration` merges `--no-ff` in a worktree of `mesh/prj_x/integration`; the positive test asserts `^2` exists, differs from `main`'s initial commit, and adds a follow-up commit so the merge is a strict ancestor | `test_i9_merged_task_with_ancestor_commit_is_clean` |
| 6 | I9: rc 1 vs other rc separated, non-sha `merge_commit` rejected before git, `refs/heads/<branch>` used, still one violation per task | `test_i9_non_sha_merge_commit_is_rejected_before_git`, `test_i9_merge_base_error_is_reported_as_git_error` |
| 7 | This section | — |

Suite after r1b: `bash scripts/test.sh` rc 0, unit 129, integration 146, recovery 1, wall time
800 s (host under concurrent worktree load; r1b adds only unit tests). Both tripwire files
absent. `python3 -m unittest tests.unit.test_gitfixture tests.unit.test_invariants -v` rc 0,
24 tests OK.

Note for later runs (from the review, no coder action here): `git()` requires `cwd` under the
fixture base, while code-task worktrees live under the harness home. The reviewer recommends
letting `GitMeshHarness` register its own home as a second allowed root; that is r3/r4 scope and
was not implemented in r1b.

## r1c — Kiro re-review fixes (review m2-r1b, items 1–6)

| Item | Change | Tests |
|---|---|---|
| 1 (MAJOR) | `GitMeshHarness.get_env()` now drops every inherited `GIT_*` key from `super().get_env()` before adding `git_env_vars()`, so an outer `GIT_DIR`/`GIT_WORK_TREE` (git hook or wrapper) or inherited `GIT_AUTHOR_*`/`GIT_COMMITTER_*` can never reach meshd. `PATH`/`MESH_HOME`/`MESH_TESTING`/`MESH_TRIPWIRE_FILE`/`MESH_SUITE_TRIPWIRE_FILE` are unchanged. `tests/helpers.py` and `mesh/constants.py` untouched. | `test_get_env_keeps_harness_env_and_adds_git_keys` (now runs under `mock.patch.dict(os.environ, …)` with `GIT_DIR`/`GIT_WORK_TREE` = a fixture-owned dir and `GIT_AUTHOR_NAME`/`GIT_COMMITTER_EMAIL` = `leak`; asserts none of them is present and that the only `GIT_*` keys are the four hermetic ones) |
| 2 (MINOR) | `branches()` no longer rewrites `*` → `**`: it lists `refs/heads/**` with `for-each-ref --format=%(refname:short)` and filters in Python with `fnmatch.fnmatchcase`, so `*` crosses `/` again. Docstring updated. | `test_branches_returns_plain_names_for_worktree_branches` gained `branches(repo, "mesh/*") == ["mesh/prj_x/tsk_1"]` |
| 3 (MINOR) | `_SHA_RE` ends with `\Z` instead of `$`, so a sha with a trailing newline is rejected before git argv. | `test_i9_non_sha_merge_commit_is_rejected_before_git` now covers two tasks (`--upload-pack=x` and `<head sha> + "\n"`), both "not a sha", no git spawn |
| 4 (MINOR) | `tests/gitfixture.py` module docstring now states exactly what `_assert_safe_args` refuses (the listed options plus *absolute* path arguments outside `/tmp/mh-`) and says relative arguments and `-c` values are not inspected. The check itself was not extended (optional in the note). | — |
| 5 | This section | — |
| 6 | Focused tests and the full suite run (below) | — |

Focused run: `python3 -m unittest tests.unit.test_gitfixture tests.unit.test_invariants -v` rc 0,
24 tests OK (6.0 s).

Suite after r1c: `bash scripts/test.sh` rc 0, unit 129, integration 146, recovery 1, wall time
reported in the table above; both tripwire files absent.

Changed assertions in r1c: none weakened. `test_i9_non_sha_merge_commit_is_rejected_before_git`
was strengthened (one task → two tasks, per-task reason asserted); the harness env test was
strengthened (leak patched into `os.environ`, exact `GIT_*` key set asserted); the `branches()`
test gained one assertion. No assertion was removed.

## Not done (out of scope for r1)

K1–K14 product behaviour, `project deliver`, `workspace prune/delete`, merge crash recovery,
lead turns, `mesh-actions`, the lead prompt and resume plumbing — all later M2 runs.
