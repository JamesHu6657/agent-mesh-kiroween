## Common block for every M2 run (Quest pastes it at the top of each prompt)

You are the IMPLEMENTER of "Agent Mesh v1" in the current directory (a git checkout of mesh-fresh-v1, usually a worktree on branch `wt/<name>`). Another coder may work in a separate worktree at the same time: work ONLY in the current directory and only on the files your run names or clearly needs.
Status: M1a, M1b and M1c are ACCEPTED (M1 exit criteria met; see docs/m1c-gate-report.md once it exists). Now implement gate **M2: Git workspaces, merge, lead turns** (plan §16 M2): repo projects (§10.2), worktrees (§10.3), submission git info (§10.4), the merge pipeline T14–T18 (§10.5), `project deliver` (§10.6), `workspace prune/delete` (§10.7), merge crash recovery (§11.7 step 5), lead turns with debounce (§12.4), the `mesh-actions` protocol, the lead prompt (§12.6) and resume plumbing (exercised with the fake adapter's `emit_session`). M2 items: checklist sections **K (K1–K14)** and **L (L1–L6)**. Every M1 item must still pass (checklist §0), including A1 (`scripts/test.sh` < 10 min), A3 (tripwire empty), A6 (gitops allow-list) and A8 (`doctor --check-invariants` clean, now with I9). B11 is superseded by section K. Verbs of M2 not yet built at a commit keep returning rc 2 `NOT_IMPLEMENTED_IN_M1` with `details.milestone:"M2"`.
The run plan is docs/coordination/plans/m2-plan.md. Read the planner notes and your run's section fully. The open questions there were decided by the coordinator as stated in your run's section.
Authoritative docs: docs/requirements.md, docs/plan.md, docs/acceptance-checklist.md (read §0 notation and every item ID you cover), docs/impl-notes-m1a.md, docs/impl-notes-m1b.md, docs/impl-notes-m1c.md (and docs/impl-notes-m1c-run2.md if present), and the docs/impl-notes-m2-r*.md files of earlier M2 runs.
Hard rules:
- Never edit docs/requirements.md, docs/plan.md, docs/acceptance-checklist.md, docs/review-*.md, docs/reviews/**, docs/coordination/**, prompts/**, scripts/coord/**, .kiro/**, docs/HANDOFF.md, docs/QUEST-HANDOFF.md, docs/release-requirements.md, docs/m1*-*.md, docs/impl-notes-m1*.md, logs/**, tests/fixtures/real/**. If the plan is ambiguous, choose the most conservative reading and record it. If something in the plan is a real blocker, STOP and report it instead of inventing scope.
- Keep ALL existing behaviour and tests green. Never weaken or delete an assertion. The only assertions you may change are the ones your run section names (stub milestones superseded by a built verb). List each one with its reason in your notes file.
- Every checklist item you cover gets a test with `ACC: <ID>` in its docstring. Where the item is about CLI behaviour, the test drives the REAL CLI path (bin/mesh subprocess against a /tmp/mh-* daemon via the harness). A test that covers only part of an item still gets the tag, and your notes list the item as partial.
- **Git safety.**
  - Mesh product code runs git ONLY through `mesh/core/gitops.py` (A6; `test_no_git_subprocesses_outside_gitops` must stay green). Mesh never pushes, never sets git identity or config, never rebases, resets, checks out, or forces.
  - Tests run git ONLY through the helpers in `tests/gitfixture.py`, always with `cwd` inside a `/tmp/mh-*` directory the test created. Never run git without an explicit cwd. Never pass the Mesh checkout (`/workspace/mesh-fresh-v1`, `/home/node/workspace/mesh-fresh-v1`, any `mesh-wt-*`) or any directory outside `/tmp/mh-*` as `--repo`, as a repo path, or as a cwd for git.
  - Do not put `GIT_AUTHOR_*` or `GIT_COMMITTER_*` into meshd's env. A test that needs a commit in a task worktree makes it with `git -c user.name=t -c user.email=t@example.invalid commit …` via the fixture helper, unless the K0 repo-local identity applies.
- Mock adapters and fixtures ONLY. Tasks and lead turns launch only on the `fake` adapter (testing mode); inbox agents use `none`. Vendor adapters stay `launch_enabled:false` (exit 13, milestone M3). No real vendor CLI runs, no inference, no login, no network, no TCP ports, no reading vendor credential or config dirs. Python stdlib only.
- Tests use /tmp/mh-* homes, /tmp/mh-* git fixtures and Unix sockets only, and clean up only what they created: homes, fixture repos, daemons, and processes whose environ carries the `MESH_RUN_ID` of a run in their own home. Never kill by name or pattern. No push (also not to the fixture's bare remote), no remote creation outside the fixture, no deploy.
- Authority is enforced by credentials and capabilities in the daemon, never by a role string. Lead run tokens act only for their project and only while their lead assignment is current.
- **Test budget.**
  - Group the Check clauses of one item, or of closely related items, into one scenario test with one harness.
  - Create only the agents the test needs.
  - Use the inbox executor `ibx` with test-made commits wherever the item does not name `fx`.
  - Use testing-mode settings instead of fixed sleeps, and poll with `wait_for` instead of `sleep`.
  - Mark `@serial` only what truly depends on wall-clock timing.
  - Report the suite wall time before your first change and after your last.
- Commit after each sub-step: `git commit -m "<type>(m2): <what> (ACC: …)" --author "agy gemini-3.8-flash-high <agy@localhost>"`, type = feat/test/docs. Run `bash scripts/test.sh` (rc 0, tripwire empty) before every commit that touches code or tests.
- agy 1.5.3 is installed while fixtures are 1.5.2; agy capabilities reported `stale` is the honest expected result. Do not change fixtures or expected hashes. If `test_fixture_hashes` fails, STOP and report.
Shell permission note: any command outside the allow-list is auto-denied and ENDS your run.
- Allowed prefixes only: `python3` (incl. `python3 -m unittest`), `bin/mesh`, `./bin/mesh`, `bash scripts/test.sh`, `scripts/test.sh`, `./scripts/test.sh`, `mkdir -p`, `ls`, `grep`, `rg`, `sha256sum`, `find .` (must literally start with `find .`; never `-delete` or `-exec`), `cat`, `head`, `tail`, `wc`, `pwd`, `sed -n`, `stat`, `file`, `diff`, `tree`, `du -sh`, `which`, `git status`, `git diff`, `git log`, `git show`, `git add`, `git commit`, `git grep`, `git blame`, `git ls-files`, `git rev-parse`, `git cat-file`, `git branch --list`.
- NOT allowed: `git checkout`/`restore`/`reset`/`stash`/`clean`/`switch`/`worktree`/`clone`/`config`/`merge` (to see an old file use `git show <ref>:<path>`; to undo your own edit, edit the file back), `rm`, `mv`, `cp`, `kill`, `pkill`, `ps`, `sleep`, `env`, `cd`, `timeout`, and `bash`/`sh` with any other script. Your shell never runs git against a temp repo; tests do that through python3.
- You cannot `chmod` new files, so do not create new executable scripts; run tools with `python3 <file>`.
- ONE simple command per call: no `&&`, `;`, `|`, redirection or `$(…)`. Prefer the file and search tools. Use python3 for cleanup only inside /tmp/mh-* paths your tests created.
- `bash scripts/test.sh` takes up to ~10 min: run it with a long timeout (900 s+) in the same call and wait for it. Never end your turn while "waiting".
Write large files in several edits of at most ~200 lines each. Be efficient: read each file you need once, no exploratory benchmarking loops.
Notes: write your readings, deviations, changed assertions (with reason), ACC coverage (full/partial, test names), the files you changed and the suite wall times into a NEW file `docs/impl-notes-m2-<run>.md` (e.g. `docs/impl-notes-m2-r3.md`). Do not edit another run's notes file.
DONE for every run, in addition to the run's own list:
1. One commit per sub-step, each with the author above.
2. The final `bash scripts/test.sh` is rc 0 with both tripwire files empty, and the total wall time is printed and under 10 minutes.
3. `grep -rn "ACC: <ID>" tests/` finds a passing test for each ID of the run.
4. No existing assertion was weakened, and every changed assertion is listed with its reason.
5. `git branch --list 'mesh/*'` in the current checkout prints nothing (no test touched the Mesh repo).
6. `docs/impl-notes-m2-<run>.md` exists.
At the end print: commits, files changed, exact test commands with exit codes, test counts and the wall time of `bash scripts/test.sh` (before and after), the ACC IDs covered (full or partial), readings/deviations, and anything not done. Then stop.

---

## Run r1 prompt (m2-r1, m2-gitfix): FULL. EARLY: may run while M1c is still in progress

[Common block above]

STATUS OVERRIDE for this run: M1c is NOT finished yet. Another coder is working on M1c (supervisor, runwrap, recovery, harness) in a separate worktree. Do not touch any of these files: mesh/daemon/**, mesh/runwrap.py, mesh/core/{prompts,redact,leases,workspaces,authz,gitops,events}.py, mesh/testing/**, mesh/adapters/**, tests/helpers.py, tests/run_tier.py, scripts/test.sh, tests/integration/**, tests/recovery/**.

THIS RUN (m2-r1): the K0 git fixture that every K test will use, and invariant I9. No product behaviour changes except the new I9 check.

Items: **A8** (the "I9 from M2" clause; partial until r5 creates real merged tasks). The fixture is supporting work for K1–K14 and has no ACC tag of its own.

START
1. `git status` must be clean. `git log --oneline -3`. Record HEAD.
2. Run `bash scripts/test.sh` once as the baseline. Record rc, the unit/integration/recovery counts, and the total wall time.
3. Read: docs/acceptance-checklist.md section K (the fixture K0 paragraph and K1–K14), plan §5.6 (I9), §10.1–§10.5, mesh/core/invariants.py, mesh/core/gitops.py, mesh/cli/commands/doctor.py, tests/unit/test_invariants.py, tests/unit/test_gitops.py, tests/helpers.py (read only: `MeshHarness.__init__`, `get_env`, teardown).

SUB-STEPS

(1) Git fixture. Commit `test(m2): K0 git fixture with hermetic git env`.
- New file `tests/gitfixture.py` (stdlib only; its name does not start with `test_`):
  - `class GitFixture` created with `GitFixture()`. It makes its own base dir `/tmp/mh-<rand8>-git/` (mode 0700) and refuses any path that does not start with `/tmp/mh-` (raise `AssertionError`). Methods:
    - `env()`: returns `os.environ` plus `GIT_CONFIG_GLOBAL=<base>/empty.gitconfig` (an empty file the fixture creates), `GIT_CONFIG_NOSYSTEM=1`, `GIT_TERMINAL_PROMPT=0`, `LANG=C.UTF-8`, `LC_ALL=C.UTF-8`. Never sets `GIT_AUTHOR_*`/`GIT_COMMITTER_*`.
    - `git(repo, *args, check=True, identity=False)`: runs `["git", *args]` with `cwd=repo` and `env()`; asserts `repo` is under the base dir; with `identity=True` it prepends `-c user.name=t -c user.email=t@example.invalid`. Returns `CompletedProcess` (text, captured).
    - `make_k0(name="R", identity=True)`: K0 per checklist section K. It runs `git init -b main <base>/<name>`, sets the repo-local `user.name`/`user.email` (test fixture config, not Mesh), writes `calc.py` with `def add(a, b): return a + b` and `tests/test_calc.py` with a passing `unittest` test, plus `tests/__init__.py`, then `git add -A` and `git commit -m init`. It creates the bare remote with `git clone --bare <R> <base>/<name>-origin.git` and `git -C R remote add origin <that path>`. **No push anywhere.** With `identity=False` it then unsets the identity and sets `user.useConfigOnly=true` (K8 copy). Returns the repo path.
    - `remote_refs(repo)`: the sorted output lines of `git ls-remote origin`.
    - `head(repo, ref="HEAD")`, `is_clean(repo)` (`status --porcelain` empty), `has_merge_head(repo)` (`rev-parse -q --verify MERGE_HEAD` rc 0), `branches(repo, pattern)`, `worktrees(repo)` (parsed `worktree list --porcelain`: list of dicts with `worktree`, `head`, `branch` or `detached:True`).
    - `cleanup()`: `shutil.rmtree(base, ignore_errors=True)` after re-checking the `/tmp/mh-` prefix.
  - `class GitMeshHarness(MeshHarness)` (import from tests.helpers): takes a `GitFixture` and overrides `get_env()` to add the git env vars from `GitFixture.env()`, so meshd and every child it launches see the same hermetic git config. Teardown order: harness teardown first, then `fixture.cleanup()`.
  - A docstring at the top states the safety rule: never point any helper at the Mesh checkout; every git call has an explicit cwd under the fixture base.
- New file `tests/unit/test_gitfixture.py` (fast, no daemon):
  - K0 repo exists on `main`; `python3 -m unittest discover -s tests -t .` with `cwd=R` passes; `git config --get user.name` in R is set.
  - `make_k0(identity=False)`: `git config --get user.name` is empty (rc 1), and a commit attempt without `-c` identity fails, which proves `useConfigOnly` and the empty global config work.
  - `remote_refs` lists `refs/heads/main` and equals R's main head.
  - The guard: `fixture.git("/home/node/workspace/mesh-fresh-v1", "status")` raises `AssertionError` **before** any process starts (patch `subprocess.run`/`Popen` to fail the test if called).
  - `cleanup()` removes the base dir.

(2) I9. Commit `feat(m2): invariant I9 for merged code tasks (ACC: A8)`.
- mesh/core/invariants.py: add I9 per plan §5.6. For each task with `kind='code' AND status='merged'`: `merge_json.merge_commit` must be present, the project must have `repo_path` and `integration_branch`, `git cat-file -e <commit>^{commit}` must succeed, and `git merge-base --is-ancestor <commit> <integration_branch>` must return 0, both via `mesh.core.gitops.run_git` with `cwd=<repo_path>`. Any failure (incl. a missing repo dir or a git error) is one violation with id `I9`, the task id and a short reason. When no merged code task exists, no git process is started. Keep the existing I1–I8 code and output format unchanged.
- tests/unit/test_invariants.py: add cases using `GitFixture` and an in-memory or temp DB built the way the existing tests build states.
  - (a) A real merge commit made by the fixture on a branch `mesh/prj_x/integration` (fixture `merge --no-ff` with identity), recorded on a merged code task → zero violations.
  - (b) A commit that exists but is not an ancestor of the integration branch → `I9`.
  - (c) A bogus sha → `I9`.
  - (d) Merged code task with `merge_json` NULL → `I9`.
  - (e) No merged code tasks → zero violations and no git subprocess (patch `mesh.core.gitops.subprocess` to fail if called).
  - Tag the test class or each test `ACC: A8` and mention "I9 clause" in the docstring.
- If the doctor CLI needs any change to call the new check, keep it minimal (doctor already calls the invariant checker; verify with `grep -n "check" mesh/cli/commands/doctor.py`). Do not change doctor's output envelope.

(3) Notes. Commit `docs(m2): notes for r1`.
- docs/impl-notes-m2-r1.md: fixture API; the hermetic-env choice and why (risk 6 of the plan: unknown global git config on this box, auto-detected identities); the no-push bare-remote construction; the I9 reading for missing repos and git errors; baseline vs final suite wall time; files changed.

FILES LIKELY TOUCHED: tests/gitfixture.py (new), tests/unit/test_gitfixture.py (new), mesh/core/invariants.py, tests/unit/test_invariants.py, docs/impl-notes-m2-r1.md (new). Nothing else.

TESTS: `python3 -m unittest tests.unit.test_gitfixture tests.unit.test_invariants -v` while iterating; `bash scripts/test.sh` before each commit.

DONE (in addition to the common list): the fixture's guard refuses a Mesh-checkout path before any spawn; I9 reports the four bad cases and stays silent (and git-free) without merged code tasks; the existing invariant tests are unchanged.

---


Commit with --author "vibe deepseek-v4.1-flash <vibe@localhost>". Use your file tools to edit; shell only the read-only allow-list, one simple command per call. At the end print commits, files, test commands + rc + counts, ACC IDs, readings, anything not done. Then stop.
