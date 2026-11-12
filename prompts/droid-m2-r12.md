You are the coder for run m2-r12 of Agent Mesh v1 (Python). Work ONLY in this git worktree (branch wt/m2-r12). Never edit docs/coordination/**, prompts/**, scripts/coord/**, docs/*HANDOFF*.md (in addition to the never-edit list below). At the end print the DONE list and stop.

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

## Run r12 (m2-r12, m2-gate): docs, reconciliation, gate report

[Common block.] Items: verification of **K1–K14, L1–L6**, and the M1 regression items **A1, A3, A6, A8**.
Sub-steps:
1. `docs(m2): usage for repo projects, merges and lead turns`. docs/usage.md:
   - repo projects (flags, `tests:"none"`, the repo-local identity requirement and `commit_failed`, Mesh never pushes or sets identity);
   - code tasks and branches;
   - merge, deliver, prune/delete (incl. the Q7 branch-kept rule);
   - launch-mode leads (debounce, lease, actions protocol and allowed cmds, backoff, `lead.unreachable`, resume only when verified, tested with the fake only, vendors from M3);
   - remove the "(M2)" markers that are now built and update the J7 launch-lead row.
   Update tests/unit/test_docs_m19.py only where the launch-lead row's "until then use a `delivery=inbox` lead" clause no longer holds; keep every other assertion and every `ACC:` tag; list the change. docs/adapters.md: one line that resume plumbing is exercised only with the fake.
2. `test(m2): reconciliation`.
   - For every K and L item, compare its tests with the Check and Expect and add any missing clause.
   - `grep -rn '"M2"' mesh/` finds no verb stub any more.
   - The B11 test reads as superseded (Q12).
   - `git branch --list 'mesh/*'` on this checkout is empty.
   - Final suite under 10 min (A1), with both tripwires empty (A3).
3. `docs(m2): M2 gate report`.
   - `docs/m2-gate-report.md` in the style of docs/m1b-gate-report.md ("Verifier: implementer self-check; gate decision by the coordinator"): one row per K1–K14 and L1–L6 with PASS/FAIL/NOT-COVERED, test names and CLI commands; the M1 regression statement (counts, wall time, tripwire, invariants incl. I9); every deviation and reading from docs/impl-notes-m2-r*.md (Q-decisions, testing-only settings and hooks, new end_reason words, the `branch -d` change).
   - `docs/impl-notes-m2.md`: an index of the run notes plus the consolidated deviation list.
FILES: docs/usage.md, docs/adapters.md, tests/unit/test_docs_m19.py, any K/L test file missing a clause, docs/m2-gate-report.md (new), docs/impl-notes-m2.md (new).

ADDITIONS FOR THIS RUN (coordinator):
A. docs/usage.md launch-lead row: vendor adapters cannot be launch-mode leads (rc 13 / turns refused, `lead.turn_refused`) until M3 evidence enables them; fake only in M2. Keep "until then use a delivery=inbox lead" for vendors.
B. Do not mark N1 as changed in M2; its supersession by N23 is recorded in the M3 gate report.
C. docs/usage.md: test commands documented there must not leave untracked files in the checkout (state the /tmp/mh-* home rule).
D. The gate report must list every coordinator ruling and deviation recorded in the run notes, including: P5 project cancel rc 6 merge_in_progress; r10 via='run_token' for actions; r10b async task.merge (queued in memory, durable re-queue at boot from r11/r11b, RECOVERY_FAILED rows); approval key on action items; lead run token approval.request own project only; r11 coordinator-approved fake golden resume expectation change; global approvals denied to lead run tokens (r9d).
E. A1: report the final suite wall time AND whether other suites were running (check `pgrep -fa run_tier.py` before and after). Do not try to speed the suite up in this run; just report.
F. Session budget: this run is large. Do sub-step 1 and 3 first if needed; if near ~150 turns, commit and write the Session handoff in docs/impl-notes-m2-r12.md.
G. Gate report template (James 2026-11-09 19:09, split gate review): docs/m2-gate-report.md must have an Author column per K/L/A item (Quest coder / droid <model> / vibe / agy, from git log --format=%an plus run notes; note that some commits carry a wrong default author, so use the run notes and docs/coordination/log.md mapping of runs to coders), a per-author commit-range table, and a short "Cross-cutting items" list (A1, A3, merged-tree integration, any item whose tests span authors). Two verdict lines at the end: Quest reviewer gate (non-Quest-coder parts) and Kiro gate (Quest-coder parts + cross-cutting), both pending.
