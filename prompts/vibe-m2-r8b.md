You are the coder for run m2-r8 of Agent Mesh v1, working ONLY in this worktree (branch wt/m2-r8). Commit with --author "vibe deepseek-v4.1-flash <vibe@localhost>". Never push. Never edit docs/requirements.md, docs/plan.md, docs/acceptance-checklist.md, docs/review-*.md, docs/reviews/**, tests/fixtures/real/**, docs/coordination/**, prompts/**, scripts/coord/**, docs/*HANDOFF*.md. Work efficiently: read only what the sub-steps need, start editing early, run focused tests after each sub-step, and the full bash scripts/test.sh once at the end (rc 0 required). Tests must never touch the Mesh checkout; use tests/gitfixture.py (GitFixture / GitMeshHarness). At the end print commits, files, test commands + rc + counts + wall time, items done, anything not done. Then stop.

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


## Run r8 (m2-r8, m2-deliver): deliver, prune, delete, no push (parallel with r6)

[Common block.] Do NOT edit merge.py, runwrap.py, supervisor.py (r6 runs in parallel).
Items: **K10, K11, K12**, A6 (regression). Coordinator decision applied: Q7.
Sub-steps:
1. `feat(m2): gitops allows branch -d for prune (ACC: A6)`. Allow exactly `["branch", "-d", <ref>]` (with `_check_ref`); `-D`, `--delete --force`, `-df`, `-m` and the rest stay rejected. Update tests/unit/test_gitops.py: new allowed/forbidden cases; `push/rebase/reset/checkout` still absent. Record that this resolves the §10.5/§10.7 inconsistency noted in impl-notes-m1c §1.4.
2. `feat(m2): project deliver (ACC: K10)`. Lead or user; category `deliver_to_base` (default `not_required`; a tightened policy → rc 7 and the existing `--approval` consume path); repo project and not `cancelled`.
   - `merge-base --is-ancestor <base> mesh/<prj>/integration` fails → rc 14 `NOT_FAST_FORWARD`, nothing changed.
   - If `worktree list --porcelain` shows a worktree W with the base branch checked out: W must be clean (else rc 14 `WORKTREE_DIRTY` `details.worktree`; record), then `merge --ff-only mesh/<prj>/integration` in W.
   - Otherwise `update-ref refs/heads/<base> <new> <old>`.
   - Event `project.delivered` `{old, new, method}`. Never push.
3. `feat(m2): workspace prune and delete (ACC: K12)`.
   - `workspace prune [--project] [--dry-run]` (lead or user): only code tasks `merged`, or `cancelled` with zero commits beyond `base_commit` → `worktree remove <path>` (no force; failure → skipped with reason), then `branch -d <branch>` run in the integration worktree. The response lists `removed` and `skipped` with reasons; a dry run lists only. Plain dirs are never pruned.
   - `workspace delete TSK`: gated `delete` for agents (rc 7 `category:"delete"`); the user acts directly. A task with a lease → rc 6 `RUN_STILL_ACTIVE`. Then `shutil.rmtree` of the task dir after a realpath containment check under `workspaces.root`, `worktree prune`, **branch kept** (Q7). Event `workspace.deleted`.
   - Remove the `"M2"` stubs for `project.deliver`, `workspace.prune` and `workspace.delete`.
4. `test(m2): deliver, prune and no-push tests (ACC: K10, K11, K12)`. New tests/integration/test_deliver_prune.py (inbox executor, `--test-command none`):
   - K10: after a merge, deliver → rc 0 and R's `main` == integration; advance `main` in R with a fixture commit, merge another task, deliver → rc 14 `NOT_FAST_FORWARD`; `main` unchanged.
   - K11: one scenario covering repo intake → merge → deliver → prune with `remote_refs` equal before and after.
   - K12: prune dry-run lists; prune removes only the merged and the commit-less cancelled worktrees and their branches; an accepted-but-unmerged worktree is still there; `workspace delete <unmerged> [@lead1]` → rc 7 `category:"delete"`; nothing was deleted automatically.
5. `docs(m2): notes for r8`.
FILES: mesh/core/gitops.py, tests/unit/test_gitops.py, mesh/core/repos.py, mesh/daemon/handlers/project.py (deliver), the workspace handler module M1c created for `workspace list/show` (find it with `grep -rn "workspace.list" mesh/daemon`), mesh/cli/commands/project.py, mesh/cli/commands/workspace.py, mesh/daemon/server.py (planned map only), tests/integration/test_deliver_prune.py (new), tests/unit/test_skeleton.py and tests/integration/test_daemon.py (stub probes), docs/impl-notes-m2-r8.md.


THIS RUN (m2-r8b, branch wt/m2-r8): fix the Quest review of r8 (VERDICT REVISE). Allowed files: the r8 FILES list plus mesh/core/authz.py (already touched), new docs/impl-notes-m2-r8 section r8b in docs/impl-notes-m2-r8.md. Do not weaken existing assertions; keep new tests inside existing harness scenarios where possible (suite is at the A1 10-min edge). Start editing within 15 steps.

Findings: MAJOR-1 any non-run-token agent can prune every project: authz.py:264-265 checks lead only if project_id set; run.py:423-434 prune calls check_authz with project_id=None when --project omitted and _prune_candidates (:358) covers all projects (ibx executor and grok intake-only got rc 0 and grok actually removed a worktree). MINOR-1 delete outside containment (run.py:557) returns rc 0 and records workspace.deleted. MINOR-2 deliver twice records a no-op event (project.py:1296). MINOR-3 prune not idempotent in report (already-pruned tasks listed as skipped branch_delete_failed). MINOR-4 merge-base uses short names (repos.py:241), tag can shadow branch. MINOR-5 deliver dry run loses its plan (project.py:1286). MINOR-6 authz.py scope.

Fix notes:
1. authz.py rule 7b: for workspace.prune/workspace.delete with a non-user principal and project_id None -> refuse (rc 2 INVALID_ARGUMENT details.param "project", i.e. agents must pass --project); only the user may prune unscoped. Keep project_id lead check.
2. Test (ACC: K12): ibx and grok run workspace prune without --project -> refused, merged worktree still on disk; lead of project A running prune without --project refused, B's worktree stays.
3. handle_workspace_delete: if realpath(workspace_path) is not strictly under root -> error with details.reason "outside_workspaces_root", no event, nothing deleted. Test: replace the task dir with a symlink pointing outside root before delete; assert target survives and rc != 0.
4. deliver_to_base: if old == new return method "noop" with no git write; handler skips project.delivered for noop. Test: deliver twice -> second rc 0 noop, exactly 1 event.
5. prune: candidates whose worktree dir and branch are both gone are done; omit from skipped. Test: prune twice -> second removed [] skipped [].
6. repos.py:241: use refs/heads/<base> and refs/heads/<integration> in merge-base --is-ancestor (and the ff merge).
7. deliver dry run reports {project_id, base_branch, integration_branch, fast_forward} via transitions.
8. Fix notes deviation 2, list authz.py scope. Run bash scripts/test.sh (rc 0), report wall time. Commits "fix(m2): review m2-r8 ..." with author "vibe deepseek-v4.1-flash <vibe@localhost>". Print commits, files, test rc + counts + wall time, items done, not done. Then stop.
