You are the coder for run m2-r10 of Agent Mesh v1 (Python). Work ONLY in this git worktree (branch wt/m2-r10). Commit with --author "droid claude-opus-5-5 <droid@localhost>". Never edit docs/coordination/**, prompts/**, scripts/coord/**, docs/*HANDOFF*.md (in addition to the never-edit list below). At the end print the DONE list and stop.

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
  - Tests run git ONLY through the helpers in `tests/gitfixture.py`, always with `cwd` inside a `/tmp/mh-*` directory the test created. Never run git without an explicit cwd. Never pass the Mesh checkout (`/workspace/mesh-fresh-v1`, `/home/user/workspace/mesh-fresh-v1`, any `mesh-wt-*`) or any directory outside `/tmp/mh-*` as `--repo`, as a repo path, or as a cwd for git.
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

## Run r10 (m2-r10, m2-leadactions): actions and set-lead termination

[Common block.] Items: **L2, L4**. Coordinator decisions applied: Q8, Q10, plus: on `set-lead`, also supersede the old lead's `delivering` messages and re-create them for the new lead.
Sub-steps:
1. `feat(m2): fake lead_actions behaviour and action execution (ACC: L2)`.
   - mesh/testing/fake_agent.py: `lead_actions` prints `params["actions"]` as a fenced `mesh-actions` block, then exits 0.
   - lead_turns.py: after exit 0 and before closing the run (Q10), `parse_actions` (r2) on the parsed final text. For each item in order: an item error → failure; otherwise dispatch internally as the lead run principal (`via='lead_turn'`, idempotency key `act:<run>:<i>`), with the same authz and the `LEAD_SUPERSEDED` check.
   - Record event `lead.action_result` per item `{index, cmd, ok, exit_code, error_code}`. For each failure, write an outbox message `lead.action_failed` to the current lead on `lead_wake` (dedup `lead.action_failed:<run>:<index>`; add the fan-out row in core/events.py).
   - Then ack the messages and close the run.
2. `feat(m2): set-lead terminates the old lead's turn (ACC: L4)`. `set-lead` effect: §11.4 termination of an active `leadturn:<prj>` run of the old assignment (end_reason `lead_superseded`; record the new end_reason word); its `delivering` messages are superseded and re-created for the new lead; no actions of a terminated turn are executed.
3. `test(m2): lead action and supersede tests (ACC: L2, L4)`. New tests/integration/test_lead_actions.py:
   - L2: goal `FAKE {"lead_actions":{"actions":[{"cmd":"task.create","args":{…,"assignee":"fx"}},{"cmd":"config.set","args":{…}}]}}` → the task exists (created by the lead principal), a `lead.action_result` with ok; `config.set` → `lead.action_failed`; trigger the next turn with an intake follow-up and assert its `prompt.md` contains `lead.action_failed`.
   - L4: `flead` with `FAKE {"ignore_sigterm":{}}` and `kill.grace_s=5`. Read the lead run token from `/proc/<pid>/environ` of the turn process (the M1c G18 helper). `set-lead $PRJ lead1 [U]`. Within the grace window, `task create … --token-file <captured>` → rc 9 `LEAD_SUPERSEDED`. After the kill, the same call → rc 3 (revoked). The run is `killed` with end_reason `lead_superseded`, and no `lead.action_result` exists for it.
4. `docs(m2): notes for r10`.
FILES: mesh/testing/fake_agent.py, mesh/daemon/lead_turns.py, mesh/core/lead_actions.py (dispatch mapping only), mesh/daemon/server.py (internal dispatch entry, if needed), mesh/daemon/handlers/project.py (set-lead effect), mesh/core/events.py (fan-out row), tests/integration/test_lead_actions.py (new), docs/impl-notes-m2-r10.md.


ADDITIONS FOR THIS RUN (coordinator, from reviews; apply in addition to the plan sub-steps):
A. B2 vendor lead-turn launch gate (required). A lead turn starts only if the lead adapter passes the AG step-4 launch check (same check as mesh/daemon/handlers/task.py:342-347, _validate_assignment_guards); otherwise leave the messages pending, start no run, and record one event per assignment, e.g. lead.turn_refused {reason:"adapter_not_launch_enabled"}. set-lead / project create with a delivery=launch lead whose adapter is not launch-enabled -> rc 13 ADAPTER_UNSUPPORTED (details.milestone). Put the check in one helper in lead_turns.py (e.g. lead_launch_allowed(conn, adapter, testing_mode)) so later evidence gates plug in there. Tests in tests/integration/test_lead_actions.py (default suite, shims on): a launch lead on adapter droid -> set-lead/project create rc 13; an existing launch-mode vendor lead (inserted through the testing DB path) gets no lead_turn run, messages stay pending, tripwire empty. Fake lead path unchanged. If the rc 13 check needs handlers/agent.py or handlers/task.py, STOP and report.
B. Lead run token approval.request (spec gap from review m2-r9c NEW-3): mesh/daemon/handlers/approval.py (~620) raises CREDENTIAL_NOT_ALLOWED_IN_RUN for any run token; docs/plan.md §7.3:496 and §12.4:860 allow approval.request for a lead run token. Allow it for lead run tokens on their scope project only (another project -> rc 4 SCOPE_MISMATCH); executor run tokens stay CREDENTIAL_NOT_ALLOWED_IN_RUN. This is a prerequisite for approval.request in the lead actions allowlist. Add approval.py (that hunk only) to FILES. Test: own project rc 0 with requested_by = lead agent; other project rc 4; executor token unchanged.
Keep new tests lean (the idle suite is ~445 s, budget 600 s): reuse harnesses, poll, never fixed sleeps inside token-bearing turns (use long FAKE sleeps and do user-side setup before starting the turn).
